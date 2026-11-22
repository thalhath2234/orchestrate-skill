#!/usr/bin/env python3
"""ui_review.py: screenshots of the job's web app for the UI reviewer. Standard library only.

A minimal Chrome DevTools Protocol (CDP) driver: it launches headless Chrome with
--remote-debugging-pipe (no port another local process could reach) and a throwaway profile,
emulates a desktop or mobile viewport, loads each page, runs the page's steps, and saves
full-page PNGs with what the browser saw (HTTP status of the document on screen, console
errors, uncaught exceptions, failed requests, step errors). The small RFC 6455 websocket client
below is kept for driving a Chrome that only offers a debugging port.

The harness (orchestrate.py) calls capture(plan, job, round): it starts plan.ui.dev in the
integration worktree (one dev server per state dir at a time, in its own process group, killed
afterwards, also on SIGTERM/SIGHUP, and reaped on the next run after a SIGKILL), waits until
plan.ui.url answers, and writes the shots to <integration_wt>/.orchestrate/screenshots/round-<n>/
(git-ignored by .orchestrate/.gitignore). Anything the dev server writes into the worktree is
put back afterwards. JavaScript dialogs (alert, confirm, prompt) are accepted and recorded. A page
that stops responding is recorded in "page_failures" and the capture goes on with the next page.
It never raises for an expected failure: the returned dict's "error" says why it could not run,
and the harness fails the review closed.

Manual use:
  python3 ui_review.py chrome                       which Chrome/Chromium would be used
  python3 ui_review.py capture --url http://127.0.0.1:5173 --page / [--page /about]
                               [--viewport desktop] --out DIR [--dev "pnpm dev" --cwd DIR]
  A --page may also be a JSON page: '{"path": "/", "steps": [{"click": "#go"}]}'.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jail  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
    "mobile": {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True},
}
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
CHROME_NAMES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_READY_TIMEOUT_S = 180
WAIT_FOR_S = 10.0           # a wait_for / click / type selector must appear within this
LOAD_TIMEOUT_S = 60.0       # first load on a dev server can include a compile
MAX_SHOT_HEIGHT = 4000      # CSS px; taller pages are cut here ("full-page-ish")
TILE_OVER = 1.5             # a page taller than this many viewports also gets viewport-sized tiles
MAX_LIST = 50               # messages kept per list per shot
MAX_MSG = 500               # chars per message
LOCK_WAIT_S = 900           # how long to wait for another job's dev server to finish

UI_KEYS = {"dev", "url", "ready_timeout_s", "paths", "viewports", "pages"}
PAGE_KEYS = {"path", "name", "steps"}
STEP_KINDS = ("click", "type", "press", "wait_for", "wait_ms", "screenshot")
NAME = re.compile(r"^(?=.{1,41}$)[a-z0-9]+(?:-[a-z0-9]+)*$")  # no "--": it separates the parts of a shot id
CONSOLE_QUEUE_MAX = 500     # console events queued per tab between reads; the rest are counted, not kept
# JS: element e is visible to a user: it has a box and neither it nor an ancestor is display:none,
# visibility:hidden or opacity 0. A toast that is always in the DOM but hidden doesn't count.
VISIBLE_JS = ("(e=>{const r=e.getBoundingClientRect();if(!(r.width>0&&r.height>0))return false;"
              "if(typeof e.checkVisibility==='function')return e.checkVisibility({checkOpacity:true,"
              "checkVisibilityCSS:true,opacityProperty:true,visibilityProperty:true});"
              "const s=getComputedStyle(e);return s.visibility!=='hidden'&&s.display!=='none'&&"
              "parseFloat(s.opacity||'1')>0})")
# key name -> (windowsVirtualKeyCode, code, text)
KEYS = {
    "Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", ""), "Escape": (27, "Escape", ""),
    "Backspace": (8, "Backspace", ""), "Delete": (46, "Delete", ""), "Space": (32, "Space", " "),
    "ArrowUp": (38, "ArrowUp", ""), "ArrowDown": (40, "ArrowDown", ""),
    "ArrowLeft": (37, "ArrowLeft", ""), "ArrowRight": (39, "ArrowRight", ""),
    "Home": (36, "Home", ""), "End": (35, "End", ""), "PageUp": (33, "PageUp", ""),
    "PageDown": (34, "PageDown", ""),
}


class UIUnavailable(Exception):
    """UI review cannot run (no Chrome, dev server not ready, port taken, browser crashed)."""


class CDPError(Exception):
    pass


class StepError(Exception):
    pass


LOG_FILE: Path | None = None  # the harness points this at the job's _orchestrator.log


def log(msg: str) -> None:
    line = f"[ui_review {time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    if LOG_FILE is not None:
        with contextlib.suppress(OSError), open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")


def _clip(s, n: int = MAX_MSG) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


# ================================================================== chrome ===
def find_chrome() -> str | None:
    """$ORCHESTRATE_CHROME (authoritative when set), then Chrome/Chromium on PATH, then macOS."""
    env = os.environ.get("ORCHESTRATE_CHROME")
    if env:
        found = shutil.which(env) or (env if os.path.isfile(env) and os.access(env, os.X_OK) else None)
        return found
    for n in CHROME_NAMES:
        p = shutil.which(n)
        if p:
            return p
    if os.path.isfile(MAC_CHROME) and os.access(MAC_CHROME, os.X_OK):
        return MAC_CHROME
    return None


def chrome_missing() -> str:
    """Why find_chrome() found nothing, naming what it looked at."""
    env = os.environ.get("ORCHESTRATE_CHROME")
    if env:
        return (f"no Chrome or Chromium found: $ORCHESTRATE_CHROME is set to {env!r}, which is not an "
                "executable (when it is set, nothing else is tried); fix it or unset it")
    return ("no Chrome or Chromium found (looked at " + ", ".join(CHROME_NAMES) + f" on PATH and {MAC_CHROME}); "
            "install one or set ORCHESTRATE_CHROME=/path/to/chrome")


# =============================================================== websocket ===
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def _xor(payload: bytes, key: bytes) -> bytes:
    if not payload:
        return b""
    n = len(payload)
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes, *, fin: bool = True, mask: bool = True,
                 mask_key: bytes | None = None) -> bytes:
    """One RFC 6455 frame. Clients must mask (section 5.3); 7/16/64-bit payload lengths."""
    head = bytearray([(0x80 if fin else 0) | (opcode & 0x0F)])
    mbit = 0x80 if mask else 0
    n = len(payload)
    if n < 126:
        head.append(mbit | n)
    elif n < 1 << 16:
        head.append(mbit | 126)
        head += struct.pack(">H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack(">Q", n)
    if mask:
        key = mask_key or secrets.token_bytes(4)
        return bytes(head) + key + _xor(payload, key)
    return bytes(head) + payload


def parse_frame(buf) -> tuple[bool, int, bytes, int] | None:
    """(fin, opcode, payload, bytes consumed) for the first complete frame in buf, or None when
    buf doesn't hold a whole frame yet. Nothing is consumed until a frame is complete, so a read
    timeout never leaves the stream half-read."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    fin, opcode, masked, n = bool(b0 & 0x80), b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F
    i = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n = struct.unpack(">H", bytes(buf[2:4]))[0]
        i = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n = struct.unpack(">Q", bytes(buf[2:10]))[0]
        i = 10
    key = b""
    if masked:
        if len(buf) < i + 4:
            return None
        key = bytes(buf[i:i + 4])
        i += 4
    if len(buf) < i + n:
        return None
    payload = bytes(buf[i:i + n])
    return fin, opcode, (_xor(payload, key) if masked else payload), i + n


class WebSocket:
    """Just enough RFC 6455 for a CDP client: text messages, fragmentation, ping/pong, close."""

    def __init__(self, sock: socket.socket, leftover: bytes = b""):
        self.sock = sock
        self.buf = bytearray(leftover)
        self.closed = False

    @classmethod
    def connect(cls, url: str, timeout: float = 10.0) -> "WebSocket":
        u = urllib.parse.urlparse(url)
        if u.scheme != "ws":
            raise ValueError(f"only ws:// URLs are supported, got {url!r}")
        host, port = u.hostname or "127.0.0.1", u.port or 80
        sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode())
        data = b""
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in data:
            if time.monotonic() > deadline:
                sock.close()
                raise ConnectionError("websocket handshake timed out")
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise ConnectionError("websocket handshake: connection closed")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if " 101 " not in lines[0] + " " or headers.get("sec-websocket-accept") != want:
            sock.close()
            raise ConnectionError(f"websocket handshake refused: {lines[0]}")
        return cls(sock, rest)

    def send(self, opcode: int, payload: bytes) -> None:
        self.sock.settimeout(30)
        self.sock.sendall(encode_frame(opcode, payload))

    def send_text(self, text: str) -> None:
        self.send(OP_TEXT, text.encode())

    def _frame(self, deadline: float) -> tuple[bool, int, bytes]:
        while True:
            f = parse_frame(self.buf)
            if f is not None:
                del self.buf[:f[3]]
                return f[0], f[1], f[2]
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(1 << 20)
            except socket.timeout:
                raise TimeoutError from None
            if not chunk:
                self.closed = True
                raise ConnectionError("websocket closed by the browser")
            self.buf += chunk

    def recv(self, timeout: float) -> str:
        """Next whole text message. Raises TimeoutError (stream intact) or ConnectionError.
        A timeout between fragments drops the partial message, so pass a generous timeout."""
        deadline = time.monotonic() + timeout
        parts: list[bytes] = []
        while True:
            fin, op, payload = self._frame(deadline)
            if op == OP_PING:
                self.send(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                self.closed = True
                with contextlib.suppress(OSError):
                    self.send(OP_CLOSE, payload[:2])
                raise ConnectionError("websocket closed by the browser")
            if op in (OP_TEXT, OP_BINARY):
                parts = [payload]
            elif op == OP_CONT:
                parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        if not self.closed:
            with contextlib.suppress(OSError):
                self.send(OP_CLOSE, struct.pack(">H", 1000))
        self.closed = True
        with contextlib.suppress(OSError):
            self.sock.close()


class PipeConn:
    """CDP over --remote-debugging-pipe: NUL-terminated JSON messages, written to the pipe Chrome
    reads as fd 3 and read from the one it writes as fd 4. Same interface as WebSocket."""

    def __init__(self, rfd: int, wfd: int):
        self.rfd, self.wfd = rfd, wfd
        self.buf = bytearray()
        self.closed = False

    def send_text(self, text: str) -> None:
        data = memoryview(text.encode() + b"\0")
        try:
            while data:
                data = data[os.write(self.wfd, data):]
        except OSError as e:
            self.closed = True
            raise ConnectionError(f"Chrome's DevTools pipe is closed: {e}") from None

    def recv(self, timeout: float) -> str:
        """Next whole message. Raises TimeoutError (stream intact) or ConnectionError."""
        deadline = time.monotonic() + timeout
        while True:
            i = self.buf.find(b"\0")
            if i >= 0:
                msg = bytes(self.buf[:i])
                del self.buf[:i + 1]
                return msg.decode("utf-8", "replace")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            if not select.select([self.rfd], [], [], left)[0]:
                raise TimeoutError
            chunk = os.read(self.rfd, 1 << 20)
            if not chunk:
                self.closed = True
                raise ConnectionError("Chrome closed its DevTools pipe")
            self.buf += chunk

    def close(self) -> None:
        self.closed = True
        for fd in (self.rfd, self.wfd):
            with contextlib.suppress(OSError):
                os.close(fd)
        self.rfd = self.wfd = -1


# ===================================================================== cdp ===
class CDP:
    """Browser-level CDP connection with flattened target sessions."""

    def __init__(self, ws):
        self.ws = ws  # a PipeConn (or a WebSocket): send_text, recv(timeout), close, closed
        self.next_id = 0
        self.events: deque = deque()
        self.console_queued: dict = {}   # session -> console events queued since the last take()
        self.console_dropped: dict = {}  # session -> console events dropped past CONSOLE_QUEUE_MAX

    def send(self, method: str, params: dict | None = None, session: str | None = None) -> int:
        """Send a command without waiting; its answer is skipped when it arrives."""
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send_text(json.dumps(msg))
        return self.next_id

    def _event(self, m: dict) -> None:
        """Queue an event. A JavaScript dialog (alert, confirm, prompt, beforeunload) blocks the page
        until it is answered, so it is accepted here, at once, from whatever loop is reading.
        A page that logs in a loop can't grow the queue without bound: past CONSOLE_QUEUE_MAX
        console events per tab (between reads) the rest are only counted."""
        if m.get("method") in ("Runtime.consoleAPICalled", "Log.entryAdded"):
            sid = m.get("sessionId")
            n = self.console_queued.get(sid, 0)
            if n >= CONSOLE_QUEUE_MAX:
                self.console_dropped[sid] = self.console_dropped.get(sid, 0) + 1
                return
            self.console_queued[sid] = n + 1
        self.events.append(m)
        if m.get("method") == "Page.javascriptDialogOpening":
            with contextlib.suppress(OSError):
                self.send("Page.handleJavaScriptDialog", {"accept": True}, m.get("sessionId"))

    def call(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 30.0) -> dict:
        mid = self.send(method, params, session)
        deadline = time.monotonic() + timeout
        while True:
            try:
                m = json.loads(self.ws.recv(max(deadline - time.monotonic(), 0.001)))
            except TimeoutError:
                raise CDPError(f"{method}: no answer from Chrome within {timeout:.0f}s") from None
            if m.get("id") == mid:
                if "error" in m:
                    raise CDPError(f"{method}: {(m['error'] or {}).get('message', m['error'])}")
                return m.get("result") or {}
            if "method" in m:
                self._event(m)

    def pump(self, seconds: float) -> None:
        """Collect events for up to `seconds` (returns early once the socket is quiet)."""
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:
                m = json.loads(self.ws.recv(left))
            except TimeoutError:
                return
            if "method" in m:
                self._event(m)

    def take(self) -> list[dict]:
        out = list(self.events)
        self.events.clear()
        self.console_queued.clear()
        return out


class Chrome:
    """Headless Chrome with a temp profile; always killed and its profile removed on exit."""

    def __init__(self, binary: str | None = None):
        self.binary = binary or find_chrome()
        if not self.binary:
            raise UIUnavailable(chrome_missing())
        self.proc: subprocess.Popen | None = None
        self.tmp: str | None = None
        self.cdp: CDP | None = None
        self._log = None

    def __enter__(self) -> "Chrome":
        try:
            self._start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _start(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="orchestrate-chrome-")
        args = ["--headless=new", "--remote-debugging-pipe",
                f"--user-data-dir={self.tmp}", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-background-networking", "--disable-sync",
                "--disable-component-update", "--disable-default-apps", "--hide-scrollbars",
                "--mute-audio", "--disable-dev-shm-usage", "--password-store=basic",
                "--use-mock-keychain", "--ignore-certificate-errors"]  # a local https dev server
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")  # Chrome refuses to sandbox as root (containers)
        args.append("about:blank")
        self._log = open(Path(self.tmp) / "chrome.log", "wb")
        # Chrome reads commands on fd 3 and writes replies on fd 4. Our ends of the child's pipes
        # are moved above fd 9 first, so putting them at 3 and 4 can't clobber one with the other.
        r_cmd, w_cmd = os.pipe()
        r_out, w_out = os.pipe()
        child = []
        try:
            for fd in (r_cmd, w_out):
                child.append(fcntl.fcntl(fd, fcntl.F_DUPFD, 10))
                os.close(fd)
            launcher = ("import os,sys\nr,w=int(sys.argv[1]),int(sys.argv[2])\nos.dup2(r,3)\nos.dup2(w,4)\n"
                        "os.close(r)\nos.close(w)\nos.execv(sys.argv[3],sys.argv[3:])")
            try:
                self.proc = subprocess.Popen([sys.executable, "-c", launcher, str(child[0]), str(child[1]),
                                              self.binary, *args], stdout=self._log, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, start_new_session=True, pass_fds=child)
            except OSError as e:
                os.close(w_cmd)
                os.close(r_out)
                raise UIUnavailable(f"could not start Chrome ({self.binary}): {e}") from None
        finally:
            for fd in child:
                os.close(fd)
        _register(self.proc, "chrome", self.tmp)
        self.cdp = CDP(PipeConn(r_out, w_cmd))
        try:
            self.cdp.call("Browser.getVersion", timeout=30)
        except (CDPError, ConnectionError, OSError):
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(3)
            if self.proc.poll() is not None:
                raise UIUnavailable(f"Chrome exited with code {self.proc.returncode} before DevTools "
                                    f"came up: {self._tail()}") from None
            raise UIUnavailable(f"Chrome did not answer on its DevTools pipe within 30s: {self._tail()}") from None

    def _tail(self) -> str:
        with contextlib.suppress(OSError, TypeError):
            return _clip((Path(self.tmp) / "chrome.log").read_text(errors="replace")[-800:], 800).strip()
        return ""

    def close(self) -> None:
        if self.cdp:
            with contextlib.suppress(Exception):
                self.cdp.call("Browser.close", timeout=3)
            self.cdp.ws.close()
            self.cdp = None
        if self.proc:
            kill_group(self.proc)
            _unregister(self.proc)
            self.proc = None
        if self._log:
            self._log.close()
            self._log = None
        if self.tmp:
            remove_profile(self.tmp)
            self.tmp = None


CHROME_SCRATCH = re.compile(r"^\.?(?:com\.google\.Chrome|org\.chromium\.Chromium)\.[A-Za-z0-9_]+$")


def _chrome_scratch(profile: str) -> Path | None:
    """The temp dir Chrome keeps its SingletonSocket in (/tmp/.com.google.Chrome.XXXXXX, linked from
    the profile). Chrome removes it only on a clean exit, so a killed or crashed Chrome leaves it."""
    try:
        target = Path(os.readlink(Path(profile) / "SingletonSocket"))
    except OSError:
        return None
    d = target.parent
    temps = {Path(tempfile.gettempdir()), Path(os.environ.get("TMPDIR") or "/tmp"), Path("/tmp")}
    try:
        st = os.lstat(d)
    except OSError:
        return None
    if (target.name == "SingletonSocket" and CHROME_SCRATCH.match(d.name) and d.parent in temps
            and stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()):
        return d
    return None


def remove_profile(profile: str) -> None:
    """Remove a Chrome temp profile and the scratch dir it links to."""
    scratch = _chrome_scratch(profile)
    for _ in range(5):  # Chrome can still be flushing its profile for a moment
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)
        if not os.path.exists(profile) and not (scratch and os.path.exists(scratch)):
            break
        time.sleep(0.2)


def kill_group(p: subprocess.Popen, grace: float = 5.0) -> None:
    """SIGTERM the process group, wait, then SIGKILL whatever of the group is left."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(p.pid, signal.SIGTERM)
    try:
        p.wait(grace)
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(p.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        p.wait(5)


# =============================================================== one page ===
def _remote_str(o: dict) -> str:
    if not isinstance(o, dict):
        return str(o)
    if "value" in o:
        v = o["value"]
        return v if isinstance(v, str) else json.dumps(v)
    return str(o.get("description") or o.get("unserializableValue") or o.get("type", ""))


class Tab:
    """One page in its own browser context (fresh cookies and storage) at one viewport."""

    def __init__(self, cdp: CDP, viewport: str):
        self.cdp = cdp
        self.vp = VIEWPORTS[viewport]
        self.ctx = cdp.call("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
        self.target = cdp.call("Target.createTarget", {"url": "about:blank",
                                                       "browserContextId": self.ctx})["targetId"]
        self.session = cdp.call("Target.attachToTarget", {"targetId": self.target, "flatten": True})["sessionId"]
        for m in ("Page.enable", "Runtime.enable", "Network.enable", "Log.enable"):
            self.call(m)
        self.call("Emulation.setDeviceMetricsOverride", {k: self.vp[k] for k in
                                                         ("width", "height", "deviceScaleFactor", "mobile")})
        if self.vp["mobile"]:
            self.call("Emulation.setUserAgentOverride", {"userAgent": MOBILE_UA})
            self.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        self.console: list[str] = []
        self.notes: list[str] = []
        self.page_errors: list[str] = []
        self.failed: list[str] = []
        self.step_errors: list[str] = []
        self.dropped = 0                      # console messages dropped past CONSOLE_QUEUE_MAX
        self.status: int | None = None        # the document on screen (the main frame's latest)
        self.first_status: int | None = None  # the first document loaded
        self.doc: str | None = None           # request id of the main frame's latest document
        self.prev_status: int | None = None   # the status before a navigation that may still be cancelled
        self.missed_shots: list[str] = []     # planned {"screenshot"} steps a failed step skipped
        self.loader: str | None = None
        self.loaded = False
        self.urls: dict[str, str] = {}
        self.inflight: set[str] = set()

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        return self.cdp.call(method, params, self.session, timeout)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.cdp.call("Target.closeTarget", {"targetId": self.target}, timeout=5)
        with contextlib.suppress(Exception):
            self.cdp.call("Target.disposeBrowserContext", {"browserContextId": self.ctx}, timeout=5)
        self.cdp.take()

    @staticmethod
    def _add(lst: list, msg: str) -> None:
        if len(lst) < MAX_LIST:
            lst.append(_clip(msg))

    @staticmethod
    def _favicon(url: str) -> bool:
        """The browser asks for /favicon.ico on its own; a 404 there is noise, not the app's error."""
        return urllib.parse.urlparse(url or "").path == "/favicon.ico"

    def process(self) -> None:
        self.dropped += self.cdp.console_dropped.pop(self.session, 0)
        for ev in self.cdp.take():
            if ev.get("sessionId") != self.session:
                continue
            m, p = ev.get("method"), ev.get("params") or {}
            if m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "warning", "assert"):
                self._add(self.console, f"[{p['type']}] " + " ".join(_remote_str(a) for a in p.get("args") or []))
            elif m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails") or {}
                ex = d.get("exception") or {}
                self._add(self.page_errors, ex.get("description") or d.get("text") or "uncaught exception")
            elif m == "Log.entryAdded":
                e = p.get("entry") or {}
                if e.get("level") in ("error", "warning") and not self._favicon(e.get("url", "")):
                    self._add(self.console, f"[{e.get('source', 'log')} {e['level']}] {e.get('text', '')}"
                              + (f" ({e['url']})" if e.get("url") else ""))
            elif m == "Network.requestWillBeSent":
                rid = p.get("requestId")
                self.urls[rid] = (p.get("request") or {}).get("url", "")
                if p.get("type") == "Document" and p.get("frameId") == self.target:
                    if rid != self.doc and self.doc is not None:
                        # a new document for the page itself (a link, a form, a JS redirect): until
                        # its response arrives, nothing on screen has a status
                        self.prev_status, self.status = self.status, None
                    self.doc = rid
                if p.get("type") not in ("EventSource", "WebSocket"):  # dev-server HMR never finishes
                    self.inflight.add(rid)
            elif m == "Network.responseReceived":
                rid, r = p.get("requestId"), p.get("response") or {}
                st = r.get("status")
                if rid == self.doc or rid == self.loader or (self.status is None and p.get("type") == "Document"
                                                            and p.get("frameId") in (None, self.target)):
                    self.status = int(st) if st is not None else None
                    if self.first_status is None:
                        self.first_status = self.status
                elif isinstance(st, (int, float)) and st >= 400 and not self._favicon(r.get("url") or self.urls.get(rid, "")):
                    self._add(self.failed, f"HTTP {int(st)} {r.get('url') or self.urls.get(rid, '')}")
            elif m == "Network.loadingFinished":
                self.inflight.discard(p.get("requestId"))
            elif m == "Network.loadingFailed":
                rid = p.get("requestId")
                self.inflight.discard(rid)
                err = p.get("errorText", "failed")
                cancelled = p.get("canceled") or err == "net::ERR_ABORTED"  # a download, a 204, a stopped navigation
                if rid == self.doc and self.status is None and self.first_status is not None:
                    # a later navigation (a failed first load is reported by navigate())
                    if cancelled:
                        self.status = self.prev_status  # the old document is still on screen
                    else:  # Chrome now shows its own error page: that is no HTTP response
                        self._add(self.page_errors, f"navigation to {self.urls.get(rid, '')} failed: {err}")
                if not p.get("canceled") and not self._favicon(self.urls.get(rid, "")):
                    self._add(self.failed, f"{err} {self.urls.get(rid, '')}".strip())
            elif m == "Page.loadEventFired":
                self.loaded = True
            elif m == "Page.javascriptDialogOpening":
                self._add(self.console, f"[dialog {p.get('type', 'alert')}] {p.get('message', '')} "
                                        "(accepted by the harness)")

    def wait(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.cdp.pump(min(0.1, max(end - time.monotonic(), 0.001)))
            self.process()

    def settle(self, max_s: float = 10.0, idle_s: float = 0.5) -> None:
        """Wait until no request has been in flight for idle_s (at most max_s)."""
        end, idle_since = time.monotonic() + max_s, None
        while time.monotonic() < end:
            self.cdp.pump(0.1)
            self.process()
            now = time.monotonic()
            if self.inflight:
                idle_since = None
            elif idle_since is None:
                idle_since = now
            elif now - idle_since >= idle_s:
                return

    def navigate(self, url: str) -> None:
        r = self.call("Page.navigate", {"url": url}, timeout=LOAD_TIMEOUT_S)
        self.loader = r.get("loaderId")
        if r.get("errorText"):
            self._add(self.page_errors, f"navigation to {url} failed: {r['errorText']}")
        end = time.monotonic() + (5 if r.get("errorText") else LOAD_TIMEOUT_S)
        while not self.loaded and time.monotonic() < end:
            self.cdp.pump(0.1)
            self.process()
        if not self.loaded and not r.get("errorText"):
            self._add(self.page_errors, f"the load event did not fire within {LOAD_TIMEOUT_S:.0f}s")
        self.settle()

    def eval(self, expr: str, timeout: float = 15.0):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": True}, timeout=timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise StepError((d.get("exception") or {}).get("description") or d.get("text") or "script error")
        return (r.get("result") or {}).get("value")

    def wait_for(self, css: str, timeout: float | None = None) -> bool:
        """Wait until an element matching css is visible (VISIBLE_JS)."""
        end = time.monotonic() + (WAIT_FOR_S if timeout is None else timeout)
        q = f"Array.from(document.querySelectorAll({json.dumps(css)})).some({VISIBLE_JS})"
        while True:
            try:
                if self.eval(q, timeout=5):
                    return True
            except StepError as e:
                if "not a valid selector" in str(e):
                    raise StepError(f"{css!r} is not a valid CSS selector") from None
            except CDPError:
                pass  # mid-navigation: no context yet; try again
            if time.monotonic() >= end:
                return False
            self.wait(0.1)

    def key(self, name: str) -> None:
        code, kcode, text = KEYS.get(name, (0, "", name if len(name) == 1 else ""))
        down = {"type": "keyDown" if text else "rawKeyDown", "key": " " if name == "Space" else name,
                "code": kcode, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        if text:
            down.update(text=text, unmodifiedText=text)
        self.call("Input.dispatchKeyEvent", down)
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": down["key"], "code": kcode,
                                             "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code})

    def step(self, st: dict) -> None:
        kind = next(iter(st))
        val = st[kind]
        if kind in ("click", "type"):
            css = val if kind == "click" else val[0]
            if not self.wait_for(css):
                raise StepError(f"{kind} {css!r}: no visible element matched within {WAIT_FOR_S:.0f}s")
            if kind == "click":
                self.click(css)
            else:
                self.eval(f"(()=>{{const e=Array.from(document.querySelectorAll({json.dumps(css)})).find({VISIBLE_JS});"
                          "e.scrollIntoView({block:'center'});e.focus();return true})()")
                for ch in val[1]:
                    if ch == "\n":
                        self.key("Enter")
                        continue
                    self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": ch, "text": ch,
                                                         "unmodifiedText": ch})
                    self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})
            self.settle(max_s=5)
        elif kind == "press":
            self.key(val)
            self.settle(max_s=5)
        elif kind == "wait_for":
            if not self.wait_for(val):
                raise StepError(f"wait_for {val!r}: nothing matched within {WAIT_FOR_S:.0f}s")
        elif kind == "wait_ms":
            self.wait(int(val) / 1000)

    HIT = """(()=>{const e=Array.from(document.querySelectorAll(%s)).find(%s);
      if(!e)return {err:'no visible element matched'};window.__orchEl=e;
      e.scrollIntoView({block:'center',inline:'center'});const r=e.getBoundingClientRect();
      const x=r.left+r.width/2,y=r.top+r.height/2;
      if(x<0||y<0||x>=innerWidth||y>=innerHeight)return {err:'the element is off-screen (its centre is at '+
        Math.round(x)+','+Math.round(y)+' in a '+innerWidth+'x'+innerHeight+' viewport)'};
      const h=document.elementFromPoint(x,y);
      if(h&&h!==e&&!e.contains(h)){const d=h.tagName.toLowerCase()+(h.id?'#'+h.id:'')+
        (typeof h.className==='string'&&h.className.trim()?'.'+h.className.trim().split(/\\s+/).join('.'):'');
        return {err:'the element is covered by <'+d+'> '+JSON.stringify((h.innerText||'').trim().slice(0,60))}}
      window.__orchClicked=false;e.addEventListener('click',()=>{window.__orchClicked=true},{once:true,capture:true});
      const v=window.visualViewport||{offsetLeft:0,offsetTop:0,scale:1};
      return {x:(x-v.offsetLeft)*v.scale,y:(y-v.offsetTop)*v.scale}})()"""

    def click(self, css: str) -> None:
        """Click as a user would: the element must be on screen and on top at its centre (not under
        an overlay), then a real mouse press and release go there. When the page's zoom makes the
        point miss, the element is clicked directly (the hit test above already passed)."""
        r = self.eval(self.HIT % (json.dumps(css), VISIBLE_JS)) or {}
        if r.get("err"):
            raise StepError(f"click {css!r}: {r['err']}")
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {"type": t, "x": r["x"], "y": r["y"], "button": "left",
                                                   "buttons": 1 if t == "mousePressed" else 0,
                                                   **({"clickCount": 1} if t != "mouseMoved" else {})},
                      timeout=15)
        with contextlib.suppress(StepError, CDPError):
            if self.eval("window.__orchClicked===true", timeout=5) is False:
                self.eval("window.__orchEl.click()", timeout=5)

    def _capture(self, out: Path, y: float, w: float, h: float, zoom: float) -> None:
        r = self.call("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True, "fromSurface": True,
            "clip": {"x": 0, "y": y, "width": w, "height": h, "scale": zoom}}, timeout=60)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(r["data"]))

    def screenshot(self, out: Path, tiles_dir: Path | None = None) -> list[Path]:
        """A full-page PNG at `out` (cut at MAX_SHOT_HEIGHT). A page taller than TILE_OVER viewports
        is also saved as viewport-sized tiles in tiles_dir (returned), so a model that views a tall
        image downscaled can still read it. A mobile page laid out wider than the phone (no
        `<meta name=viewport>`) is captured whole, zoomed out to the phone's width, as a phone
        shows it, and noted."""
        with contextlib.suppress(StepError, CDPError):
            self.eval("window.scrollTo(0,0)", timeout=5)
        m = self.call("Page.getLayoutMetrics")
        size = m.get("cssContentSize") or m.get("contentSize") or {}
        layout = m.get("cssLayoutViewport") or m.get("layoutViewport") or {}
        vw, vh = self.vp["width"], self.vp["height"]
        lw = max(int(layout.get("clientWidth") or vw), vw)
        zoom = vw / lw
        if lw > vw:
            note = (f"the page is laid out {lw} CSS px wide at a {vw} px viewport (no `<meta name=viewport>`?), "
                    f"so a phone shows it zoomed out to {zoom:.0%}; the screenshot shows that")
            if note not in self.notes:
                self.notes.append(note)
        page_h = max(float(size.get("height") or 0), vh / zoom)       # layout px
        h = min(page_h, MAX_SHOT_HEIGHT / zoom)
        if page_h * zoom > MAX_SHOT_HEIGHT:
            note = f"the page is {int(page_h * zoom)} px tall; the screenshot stops at {MAX_SHOT_HEIGHT} px"
            if note not in self.notes:
                self.notes.append(note)
        self._capture(out, 0, lw, math.ceil(h), zoom)
        tiles: list[Path] = []
        if tiles_dir is not None and h * zoom > vh * TILE_OVER:
            step = vh / zoom
            y, k = 0.0, 1
            while y < h - 1:
                t = tiles_dir / f"{out.stem}--part{k}.png"
                self._capture(t, y, lw, math.ceil(min(step, h - y)), zoom)
                tiles.append(t)
                y, k = y + step, k + 1
        return tiles

    def title(self) -> str:
        try:
            return _clip(self.eval("document.title", timeout=5) or "", 200)
        except (StepError, CDPError):
            return ""


# ================================================================== pages ===
def page_slug(path: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", path.split("?")[0].split("#")[0].lower()).strip("-")[:40].strip("-")
    return s or "home"


def collect_pages(plan: dict) -> list[dict]:
    """plan.ui.pages, then every task's ui pages with its task id. Pages with the same path and
    steps are one page whose `tasks` lists every task that declared it. Names are unique (a clash
    gets -2, -3...). No pages anywhere -> "/" as "home"."""
    ui = plan.get("ui") or {}
    entries = [(p, None) for p in ui.get("pages") or []]
    entries += [(p, t.get("id")) for t in plan.get("tasks") or [] for p in t.get("ui") or []]
    out: list[dict] = []
    by_key: dict[tuple, dict] = {}
    for p, tid in entries:
        if not isinstance(p, dict) or not isinstance(p.get("path"), str):
            continue
        steps = list(p.get("steps") or [])
        key = (p["path"], json.dumps(steps, sort_keys=True))
        e = by_key.get(key)
        if e is None:
            e = {"path": p["path"], "name": p.get("name"), "steps": steps, "tasks": []}
            by_key[key] = e
            out.append(e)
        elif not e["name"] and p.get("name"):
            e["name"] = p["name"]
        if tid and tid not in e["tasks"]:
            e["tasks"].append(tid)
    if not out:
        out = [{"path": "/", "name": "home", "steps": [], "tasks": []}]
    used: set[str] = set()
    for e in out:
        base = e["name"] or page_slug(e["path"])
        name, i = base, 2
        while name in used:
            name, i = f"{base}-{i}", i + 1
        used.add(name)
        e["name"] = name
    return out


def validate_pages(pages, where: str) -> list[str]:
    if pages is None:
        return []
    if not isinstance(pages, list):
        return [f"{where} must be a list of pages ({{\"path\": \"/...\", \"name\"?, \"steps\"?}})"]
    errs = []
    for i, p in enumerate(pages):
        w = f"{where}[{i}]"
        if not isinstance(p, dict):
            errs.append(f"{w} must be an object with a \"path\"")
            continue
        if set(p) - PAGE_KEYS:
            errs.append(f"{w}: unknown key(s) {sorted(set(p) - PAGE_KEYS)}; a page has path, name, steps")
        path = p.get("path")
        if not (isinstance(path, str) and path.startswith("/") and not path.startswith("//")
                and not re.search(r"\s", path)):
            errs.append(f"{w}.path must be an app path starting with \"/\" (got {path!r})")
        if p.get("name") is not None and not (isinstance(p["name"], str) and NAME.match(p["name"])):
            errs.append(f"{w}.name must be a lowercase slug: a-z0-9 words joined by single dashes, "
                        "max 41 chars (\"--\" separates the parts of a shot id)")
        steps = p.get("steps")
        if steps is None:
            continue
        if not isinstance(steps, list):
            errs.append(f"{w}.steps must be a list")
            continue
        shots = set()
        for j, st in enumerate(steps):
            sw = f"{w}.steps[{j}]"
            if not (isinstance(st, dict) and len(st) == 1 and next(iter(st)) in STEP_KINDS):
                errs.append(f"{sw} must be one of " + ", ".join(f'{{"{k}": ...}}' for k in STEP_KINDS))
                continue
            k, v = next(iter(st.items()))
            if k in ("click", "wait_for") and not (isinstance(v, str) and v.strip()):
                errs.append(f"{sw}.{k} must be a CSS selector")
            elif k == "type" and not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) for x in v)
                                      and v[0].strip()):
                errs.append(f"{sw}.type must be [\"<css selector>\", \"<text>\"]")
            elif k == "press" and not (isinstance(v, str) and (v in KEYS or len(v) == 1)):
                errs.append(f"{sw}.press must be a single character or one of {sorted(KEYS)}")
            elif k == "wait_ms" and not (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 60000):
                errs.append(f"{sw}.wait_ms must be an integer from 0 to 60000")
            elif k == "screenshot":
                if not (isinstance(v, str) and NAME.match(v)):
                    errs.append(f"{sw}.screenshot must be a lowercase slug name: a-z0-9 words joined by "
                                "single dashes, max 41 chars")
                elif v in shots:
                    errs.append(f"{sw}.screenshot name {v!r} is used twice on this page")
                else:
                    shots.add(v)
    return errs


def validate_ui(ui) -> list[str]:
    """Errors for plan.ui (None = no UI review)."""
    if ui is None:
        return []
    if not isinstance(ui, dict):
        return ["plan.ui must be an object: {\"dev\": \"<command>\", \"url\": \"http://127.0.0.1:<port>\", ...}"]
    errs = []
    if set(ui) - UI_KEYS:
        errs.append(f"plan.ui: unknown key(s) {sorted(set(ui) - UI_KEYS)}; known: {sorted(UI_KEYS)}")
    if not (isinstance(ui.get("dev"), str) and ui["dev"].strip()):
        errs.append("plan.ui.dev is required: the command that serves the app (run in the integration worktree)")
    u = ui.get("url")
    parsed = urllib.parse.urlparse(u) if isinstance(u, str) else None
    if not (parsed and parsed.scheme in ("http", "https") and parsed.hostname):
        errs.append("plan.ui.url is required: the http(s) base URL the dev command serves, e.g. http://127.0.0.1:5173")
    elif re.search(r"\s", u) or parsed.query or parsed.fragment or parsed.params:
        errs.append(f"plan.ui.url must be a plain base URL with no spaces, query or fragment (got {u!r})")
    else:
        try:
            parsed.port  # noqa: B018  (raises on a port out of range)
        except ValueError:
            errs.append(f"plan.ui.url has an invalid port (got {u!r})")
    rt = ui.get("ready_timeout_s")
    if rt is not None and (isinstance(rt, bool) or not isinstance(rt, (int, float)) or not 1 <= rt <= 3600):
        errs.append("plan.ui.ready_timeout_s must be a number of seconds from 1 to 3600")
    paths = ui.get("paths")
    if paths is not None:
        if not (isinstance(paths, list) and paths and all(isinstance(x, str) and x for x in paths)):
            errs.append("plan.ui.paths must be a non-empty list of repo-relative globs (or left out: always review)")
        else:
            errs += [f"plan.ui.paths: {x!r} must be repo-relative" for x in paths
                     if x.startswith("/") or ".." in Path(x).parts]
            errs += [f"plan.ui.paths: {x!r} would never match a changed file: write it as git prints paths, "
                     f"with no leading './' and no spaces around it ({x.strip().lstrip('./') or 'a directory'!r})"
                     for x in paths if x != x.strip() or x.startswith("./") or x.strip() in (".", "./")]
    vps = ui.get("viewports")
    if vps is not None and not (isinstance(vps, list) and vps and all(isinstance(v, str) and v in VIEWPORTS for v in vps)
                                and len(set(vps)) == len(vps)):
        errs.append(f"plan.ui.viewports must be a non-empty subset of {list(VIEWPORTS)}")
    return errs + validate_pages(ui.get("pages"), "plan.ui.pages")


# ============================================================== dev server ===
class AppFailed(UIUnavailable):
    """The dev command exited before the app answered: on code whose dev server started before
    the job's changes (the preflight), that is the job's bug, so it becomes a finding."""


def _opener(url: str = ""):
    handlers: list = [urllib.request.ProxyHandler({})]  # never via a proxy
    if url.startswith("https:"):
        import ssl
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))  # a local dev cert
    return urllib.request.build_opener(*handlers)


def url_answers(url: str, timeout: float = 2.0) -> bool:
    """Anything HTTP answering at url (any status) counts."""
    try:
        with _opener(url).open(urllib.request.Request(url, method="GET"), timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        e.close()
        return True
    except Exception:  # noqa: BLE001  (refused, reset, timeout, bad URL: nothing usable answers)
        return False


def port_taken(url: str, timeout: float = 1.0) -> str | None:
    """The address something accepts TCP connections on for url's host and port (every address
    the host resolves to is tried), or None. A TCP accept is enough: a server that is slow to
    answer HTTP, or speaks TLS, still holds the port."""
    u = urllib.parse.urlparse(url)
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
        infos = socket.getaddrinfo(u.hostname or "127.0.0.1", port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return None
    for fam, typ, proto, _, addr in infos:
        with contextlib.suppress(OSError), socket.socket(fam, typ, proto) as so:
            so.settimeout(timeout)
            if so.connect_ex(addr) == 0:
                return f"{addr[0]}:{addr[1]}"
    return None


def _confine(plan: dict) -> Path | None:
    """The dev command runs code workers wrote: jailed like their verify commands."""
    import router
    return jail.confinement(jail.mode_of(plan), router.DIR)


def _lock_path() -> Path:
    import router
    return router.DIR / "devserver.lock"


# --- processes this module started, for a run that was SIGKILLed before its cleanup ran ---
_REGISTRY: Path | None = None  # set while the dev-server lock is held


def _started(pid: int) -> str | None:
    """The process's start time as ps prints it: with the pid, it names one process for good."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None


def _read_registry(path: Path) -> list[dict]:
    try:
        d = json.loads(path.read_text())
        return [e for e in d if isinstance(e, dict)] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _register(p: subprocess.Popen, what: str, tmp: str | None = None) -> None:
    if _REGISTRY is None:
        return
    with contextlib.suppress(OSError):
        rows = _read_registry(_REGISTRY) + [{"pid": p.pid, "what": what, "started": _started(p.pid), "tmp": tmp}]
        _REGISTRY.write_text(json.dumps(rows))


def _unregister(p: subprocess.Popen) -> None:
    if _REGISTRY is None:
        return
    with contextlib.suppress(OSError):
        rows = [e for e in _read_registry(_REGISTRY) if e.get("pid") != p.pid]
        if rows:
            _REGISTRY.write_text(json.dumps(rows))
        else:
            _REGISTRY.unlink()


def reap_stale(registry: Path) -> list[str]:
    """Kill the process groups a killed run left behind (its dev server still holding the port, its
    Chrome) and remove their temp profiles. Only a process whose pid AND start time match the
    record is touched, so a reused pid is never hit. Call with the lock held."""
    done = []
    for e in _read_registry(registry):
        pid = e.get("pid")
        if isinstance(pid, int) and e.get("started") and _started(pid) == e["started"]:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGTERM)
                time.sleep(1)
                os.killpg(pid, signal.SIGKILL)
            done.append(f"{e.get('what')} (pid {pid})")
        tmp = e.get("tmp")
        if isinstance(tmp, str) and Path(tmp).name.startswith("orchestrate-chrome-") \
                and Path(tmp).parent == Path(tempfile.gettempdir()):
            remove_profile(tmp)
    with contextlib.suppress(OSError):
        registry.unlink()
    if done:
        log("stopped what a killed run left behind: " + ", ".join(done))
    return done


@contextlib.contextmanager
def signals_raise():
    """SIGTERM and SIGHUP raise SystemExit while the block runs, so every `finally` (the dev
    server's and Chrome's process groups) runs before the process ends. Main thread only."""
    def handler(signum, frame):
        raise SystemExit(128 + signum)
    old = {}
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        try:
            old[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):  # not the main thread: the caller's own handling applies
            pass
    try:
        yield
    finally:
        for sig, h in old.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, h)


def group_alive(p: subprocess.Popen) -> bool:
    """The dev command or anything it started (its process group) is still running."""
    if p.poll() is None:
        return True
    try:
        os.killpg(p.pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


@contextlib.contextmanager
def dev_server(cmd: str, url: str, cwd: Path, log_path: Path, ready_timeout: float = DEFAULT_READY_TIMEOUT_S,
               lock_path: Path | None = None, lock_wait: float = LOCK_WAIT_S, confine: Path | None = None):
    """Run the dev command until the block ends. One dev server per state dir at a time (a flock);
    refuses to start when anything accepts connections on url's port (it would screenshot other
    code); the command runs in its own process group, which is killed afterwards, also on
    SIGTERM/SIGHUP, and recorded so a run killed with SIGKILL is cleaned up by the next one."""
    global _REGISTRY
    lock_path = Path(lock_path or _lock_path())
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lk = open(lock_path, "a")
    try:
        with signals_raise():
            end = time.monotonic() + lock_wait
            while True:
                try:
                    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > end:
                        raise UIUnavailable(f"another job's dev server held {lock_path} for {lock_wait:.0f}s") from None
                    time.sleep(1)
            registry = lock_path.with_suffix(".pids")
            reap_stale(registry)
            _REGISTRY = registry
            busy = port_taken(url)
            if busy or url_answers(url):
                raise UIUnavailable(f"something is already serving {url} ({busy or 'it answers HTTP'}) before the "
                                    "dev command started, so the screenshots would show other code; stop it "
                                    "and resume")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "ab") as logf:
                logf.write(f"\n$ {cmd}   (cwd {cwd}, {time.strftime('%Y-%m-%d %H:%M:%S')})\n".encode())
                logf.flush()
                log_from = logf.tell()
                p = subprocess.Popen(jail.shell(cmd, cwd, confine), cwd=str(cwd), stdout=logf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True,
                                     env={**os.environ, "BROWSER": "none"})
                _register(p, "dev server")
                try:
                    deadline = time.monotonic() + ready_timeout
                    while True:
                        if url_answers(url):
                            break
                        if p.poll() is not None and not group_alive(p):
                            raise AppFailed(f"the dev command exited with code {p.returncode} before {url} "
                                            f"answered (see {log_path}): {_log_tail(log_path)}")
                        if time.monotonic() > deadline:
                            raise UIUnavailable(f"the dev server was not ready at {url} within {ready_timeout:g}s "
                                                f"(see {log_path}): {_log_tail(log_path)}")
                        time.sleep(0.5)
                    if not group_alive(p):
                        raise UIUnavailable(f"the dev command exited with code {p.returncode}, yet something "
                                            f"answers at {url}: another server, so the screenshots would show "
                                            "other code")
                    other = foreign_listener(url, p, log_path, log_from)
                    if other:
                        raise UIUnavailable(f"something other than the dev command answers at {url} ({other}); "
                                            "the dev server probably moved to another port, so the screenshots "
                                            "would show other code. Stop it and resume")
                    yield p
                finally:
                    kill_group(p)
                    _unregister(p)
    finally:
        _REGISTRY = None
        with contextlib.suppress(OSError):
            fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()


PORT_IN_USE = re.compile(r"EADDRINUSE|address already in use|port \d+ is (?:already )?in use|"
                         r"is in use,? trying another|already in use", re.I)


def _proc_stat(pid: int) -> tuple[int, int, int] | None:
    """(ppid, pgid, sid) of a pid, or None when it can't be read."""
    try:
        f = Path(f"/proc/{pid}/stat").read_text()
        rest = f[f.rindex(")") + 2:].split()
        return int(rest[1]), int(rest[2]), int(rest[3])
    except (OSError, ValueError, IndexError):
        pass
    try:
        # no `sess=`: on macOS it isn't the session id. getsid is, on both.
        r = subprocess.run(["ps", "-o", "ppid=,pgid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        a = [int(x) for x in r.stdout.split()]
        if len(a) != 2:
            return None
        try:
            sid = os.getsid(pid)
        except OSError:
            sid = a[1]
        return a[0], a[1], sid
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def listener_pids(port: int) -> list[int] | None:
    """Pids of the processes listening on TCP `port` that this user can see; [] when the
    listener belongs to nobody we can see (another user, a container proxy); None when this
    can't be told here."""
    if Path("/proc/net/tcp").exists():
        inodes = set()
        for name in ("tcp", "tcp6"):
            with contextlib.suppress(OSError):
                for line in Path(f"/proc/net/{name}").read_text().splitlines()[1:]:
                    f = line.split()
                    if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                        inodes.add(f[9])
        if not inodes:
            return None
        found = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            with contextlib.suppress(OSError):
                for fd in (d / "fd").iterdir():
                    with contextlib.suppress(OSError):
                        t = os.readlink(fd)
                        if t.startswith("socket:[") and t[8:-1] in inodes:
                            found.append(int(d.name))
                            break
        return found
    if shutil.which("lsof"):
        try:
            r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True,
                               text=True, timeout=10)
            return sorted({int(x) for x in r.stdout.split() if x.isdigit()})
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def _in_group(pid: int, root: int) -> bool:
    """pid is the dev command, in its process group or session, or one of its descendants."""
    seen = set()
    while pid and pid not in seen:
        seen.add(pid)
        if pid == root:
            return True
        st = _proc_stat(pid)
        if st is None:
            return False
        ppid, pgid, sid = st
        if root in (pgid, sid):
            return True
        pid = ppid
    return False


def foreign_listener(url: str, p: subprocess.Popen, log_path: Path, log_from: int = 0) -> str | None:
    """Why the process answering at url is not the dev command, or None. A listener we can see
    that is outside the dev command's process group is foreign; so is a dev log saying the port
    was in use. A listener we can't see (another user, a container's proxy) is not judged."""
    u = urllib.parse.urlparse(url)
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None
    pids = listener_pids(port)
    if pids:
        if any(_in_group(x, p.pid) for x in pids):
            return None
        cmd = ""
        with contextlib.suppress(OSError):
            cmd = " ".join(Path(f"/proc/{pids[0]}/cmdline").read_bytes().decode(errors="replace").split("\0")).strip()
        return f"pid {pids[0]}" + (f": {cmd[:120]}" if cmd else "") + ", not started by the dev command"
    log = ""
    with contextlib.suppress(OSError), open(log_path, "rb") as fh:  # this run's part of the log only
        fh.seek(log_from)
        log = fh.read()[-20000:].decode(errors="replace")
    m = PORT_IN_USE.search(log)
    if m and str(port) in log:
        return f"the dev log says {m.group()!r}"
    return None


def _log_tail(p: Path, n: int = 400) -> str:
    try:
        return " ".join(p.read_text(errors="replace")[-n:].split())
    except OSError:
        return ""


def check_alive(p: subprocess.Popen, url: str, log_path: Path) -> None:
    if not group_alive(p):
        raise UIUnavailable(f"the dev server exited with code {p.returncode} during the capture, so the "
                            f"remaining pages could not load (see {log_path}): {_log_tail(log_path)}")


# --- the worktree the dev server runs in: whatever it writes there is put back afterwards ---
def _status(wt: Path) -> dict[str, str] | None:
    """{path: XY} of every changed or untracked (not ignored) path, or None outside a git work tree."""
    r = subprocess.run([*jail.GIT, "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=wt,
                       capture_output=True)
    if r.returncode != 0:
        return None
    out, parts, i = {}, r.stdout.decode("utf-8", "surrogateescape").split("\0"), 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        out[e[3:]] = e[:2]
        if e[0] in "RC":
            i += 1  # the rename's source path follows
    return out


def _digest(f: Path) -> str | None:
    try:
        return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
    except OSError:
        return None


def snapshot_worktree(wt: Path) -> dict | None:
    """What is changed in wt before the dev server starts: each path's status, hash and bytes."""
    st = _status(wt)
    if st is None:
        return None
    snap = {}
    for path in st:
        f = wt / path
        data = None
        with contextlib.suppress(OSError):
            if f.is_file() and f.stat().st_size <= 20 * 1024 * 1024:
                data = f.read_bytes()
        snap[path] = {"hash": _digest(f), "data": data, "exists": f.exists()}
    return snap


def restore_worktree(wt: Path, snap: dict | None) -> list[str]:
    """Put wt back to the snapshot: a tracked file the dev server changed or deleted is checked
    out again, a file it created is removed, a file that was already changed gets its old bytes
    back. Ignored files (build caches) are left alone. Returns the paths it put back."""
    if snap is None:
        return []
    now = _status(wt) or {}
    fixed = []
    for path in sorted(set(now) | set(snap)):
        f = wt / path
        if not f.parent.resolve().is_relative_to(wt.resolve()):
            continue  # the dev command swapped a directory for a link: never write or delete through it
        before = snap.get(path)
        if before is not None:
            if _digest(f) == before["hash"] and f.exists() == before["exists"]:
                continue
            if before["data"] is not None:
                f.parent.mkdir(parents=True, exist_ok=True)
                if f.is_symlink():
                    f.unlink()  # its target isn't ours to overwrite
                f.write_bytes(before["data"])
                fixed.append(path)
            elif not before["exists"]:
                with contextlib.suppress(OSError):
                    f.unlink()
                fixed.append(path)
            continue
        tracked = subprocess.run([*jail.GIT, "cat-file", "-e", f"HEAD:{path}"], cwd=wt, capture_output=True).returncode == 0
        if tracked:
            subprocess.run([*jail.GIT, "checkout", "HEAD", "--", path], cwd=wt, capture_output=True)
        else:
            with contextlib.suppress(OSError):
                if f.is_dir() and not f.is_symlink():
                    shutil.rmtree(f)
                else:
                    f.unlink()
            d = f.parent
            while d != wt and wt in d.parents:
                try:
                    d.rmdir()  # only when empty
                except OSError:
                    break
                d = d.parent
        fixed.append(path)
    return fixed


# ================================================================= capture ===
def ensure_gitignore(integ: Path) -> Path:
    """<wt>/.orchestrate/ with a .gitignore of "*": nothing under it ever shows in git status,
    scope checks or commits, without touching the repo's own .gitignore."""
    d = Path(integ) / ".orchestrate"
    gi = d / ".gitignore"
    for p in (d, gi, d / "screenshots"):
        if p.is_symlink() or (p.exists() and p.is_dir() == (p == gi)):
            raise UIUnavailable(f"{p.relative_to(integ)} in the branch is a link or not what the harness keeps "
                                "there: refusing to write or delete through it")
    d.mkdir(parents=True, exist_ok=True)
    if not gi.exists() or gi.read_text().strip() != "*":
        gi.write_text("*\n")
    return d


def shoot(base_url: str, pages: list[dict], viewports: list[str], out_dir: Path, prefix: str = "",
          chrome: str | None = None, failures: list | None = None, alive=None) -> list[dict]:
    """Screenshot every page at every viewport. Raises UIUnavailable when the browser can't run.
    A page that stops responding (its renderer hangs: an app bug) is appended to `failures` with
    the step it was on, and the capture goes on with the next page. `alive()` is called before
    each page and at the end; it raises UIUnavailable when the dev server has died."""
    out_dir.mkdir(parents=True, exist_ok=True)
    shots: list[dict] = []
    failures = failures if failures is not None else []
    pre = f"{prefix}/" if prefix else ""
    try:
        with Chrome(chrome) as br:
            for page in pages:
                for vp in viewports:
                    if alive:
                        alive()
                    try:
                        _shoot_page(br.cdp, base_url, page, vp, out_dir, pre, shots)
                    except CDPError as e:
                        if br.cdp.ws.closed or (br.proc and br.proc.poll() is not None):
                            raise
                        failures.append(_page_failure(page, vp, e))
                        log(f"{vp:7} {page['path']}: {failures[-1]['error']}")
            if alive:
                alive()
    except (CDPError, ConnectionError, OSError) as e:
        raise UIUnavailable(f"the browser failed: {type(e).__name__}: {e}") from None
    return shots


class _PageHung(CDPError):
    def __init__(self, step: int | None, cause: CDPError):
        super().__init__(str(cause))
        self.step = step


def _page_failure(page: dict, vp: str, e: CDPError) -> dict:
    step = getattr(e, "step", None)
    where = (f"at step {step} {json.dumps((page.get('steps') or [])[step - 1])}" if step
             else "while loading or taking its screenshot")
    return {"name": page["name"], "path": page["path"], "viewport": vp, "step": step,
            "tasks": list(page.get("tasks") or []),
            "error": _clip(f"the page stopped responding {where}: {e}")}


def _shoot_page(cdp: CDP, base_url: str, page: dict, vp: str, out_dir: Path, pre: str,
                shots: list[dict]) -> None:
    url = base_url.rstrip("/") + page["path"]
    tab = Tab(cdp, vp)
    cur: list = [None]  # the 1-based step running now (None: loading or the final shot)

    def shot(step: str | None) -> None:
        stem = f"{page['name']}--{vp}" + (f"--{step}" if step else "")
        tab.process()
        tiles = tab.screenshot(out_dir / f"{stem}.png", out_dir / "tiles")
        if tab.dropped:
            note = f"{tab.dropped} more console message(s) were dropped: the page logged faster than they could be kept"
            tab.notes = [n for n in tab.notes if "console message(s) were dropped" not in n] + [note]
        shots.append({"id": pre + stem, "name": page["name"], "path": page["path"], "viewport": vp,
                      "step": step, "file": f"{pre}{stem}.png", "status": tab.status,
                      **({"first_status": tab.first_status} if tab.first_status != tab.status else {}),
                      "title": tab.title(),
                      "console": list(tab.console), "page_errors": list(tab.page_errors),
                      "failed_requests": list(tab.failed), "step_errors": list(tab.step_errors),
                      **({"missed_shots": list(tab.missed_shots)} if tab.missed_shots and step is None else {}),
                      "notes": list(tab.notes), "tiles": [f"{pre}tiles/{t.name}" for t in tiles],
                      "tasks": list(page.get("tasks") or [])})

    try:
        log(f"{vp:7} {url}")
        tab.navigate(url)
        steps = page.get("steps") or []
        for i, st in enumerate(steps):
            cur[0] = i + 1
            if "screenshot" in st:
                shot(st["screenshot"])
                continue
            try:
                tab.step(st)
            except StepError as e:
                tab.step_errors.append(_clip(f"step {i + 1} {json.dumps(st)}: {e}"))
                if i + 1 < len(steps):
                    tab.step_errors.append(f"steps {i + 2}-{len(steps)} were not run")
                tab.missed_shots = [str(s["screenshot"]) for s in steps[i + 1:] if "screenshot" in s]
                break
        cur[0] = None
        tab.settle(max_s=3)
        shot(None)
    except CDPError as e:
        raise _PageHung(cur[0], e) from None
    finally:
        tab.close()


def capture(plan: dict, job_like, round_no: int, chrome: str | None = None) -> dict:
    """Start plan.ui.dev in the integration worktree, screenshot every page, stop the server and
    put back whatever it wrote in the worktree. Returns the round's "ui" dict; "error" says why it
    could not run (never raises for that), and "app_failed" marks a dev command that exited before
    the app answered (the job's code, when the preflight passed)."""
    ui = plan["ui"]
    integ = Path(job_like.state["integration_wt"])
    root = ensure_gitignore(integ) / "screenshots"
    rdir = root / f"round-{round_no}"
    if rdir.is_symlink():
        rdir.unlink()  # never through it
    shutil.rmtree(rdir, ignore_errors=True)
    rdir.mkdir(parents=True, exist_ok=True)
    res = {"ran": False, "skipped": None, "error": None, "app_failed": False, "url": ui["url"], "dir": str(rdir),
           "shots": [], "page_failures": [], "restored": [], "verdict": None}
    pages = collect_pages(plan)
    viewports = list(ui.get("viewports") or VIEWPORTS)
    devlog = Path(job_like.dir) / "logs" / "_devserver.log"
    snap = None
    try:
        with signals_raise():
            try:
                binary = chrome or find_chrome()
                if not binary:
                    raise UIUnavailable(chrome_missing())
                snap = snapshot_worktree(integ)
                log(f"starting the dev server: {ui['dev']}  (waiting for {ui['url']})")
                with dev_server(ui["dev"], ui["url"], integ, devlog,
                                float(ui.get("ready_timeout_s") or DEFAULT_READY_TIMEOUT_S),
                                confine=_confine(plan)) as p:
                    res["shots"] = shoot(ui["url"], pages, viewports, rdir, f"round-{round_no}", binary,
                                         failures=res["page_failures"],
                                         alive=lambda: check_alive(p, ui["url"], devlog))
                res["ran"] = True
            except AppFailed as e:
                res["error"], res["app_failed"] = str(e), True
            except UIUnavailable as e:
                res["error"] = str(e)
            except Exception as e:  # noqa: BLE001  (fail closed: the caller turns this into a reviewer error)
                res["error"] = f"{type(e).__name__}: {e}"
            finally:
                res["restored"] = restore_worktree(integ, snap)
    finally:
        if res["restored"]:
            log("put back what the dev server wrote in the worktree: " + ", ".join(res["restored"][:20]))
    return res


def check_chrome(chrome: str | None = None) -> str | None:
    """Start Chrome once, open a tab on about:blank and take one screenshot. Returns why that
    failed, or None."""
    try:
        with signals_raise(), Chrome(chrome) as br:
            tab = Tab(br.cdp, "desktop")
            try:
                data = base64.b64decode(tab.call("Page.captureScreenshot", {"format": "png"}, timeout=30)["data"])
            finally:
                tab.close()
        if not data.startswith(b"\x89PNG"):
            return "Chrome took a screenshot that is not a PNG"
    except UIUnavailable as e:
        return str(e)
    except (CDPError, ConnectionError, OSError, KeyError, ValueError, TypeError) as e:
        return f"{type(e).__name__}: {e}"
    return None


def preflight(plan: dict, job_like) -> str | None:
    """Before any work: nothing may already serve plan.ui.url, the dev command must bring the
    app up on the code as it is now (the integration worktree at the job's start), and Chrome must
    start and take a screenshot. Returns why not, or None. The worktree is put back afterwards."""
    ui = plan["ui"]
    integ = Path(job_like.state["integration_wt"])
    snap = snapshot_worktree(integ)
    try:
        with signals_raise():
            try:
                with dev_server(ui["dev"], ui["url"], integ, Path(job_like.dir) / "logs" / "_devserver.log",
                                float(ui.get("ready_timeout_s") or DEFAULT_READY_TIMEOUT_S),
                                confine=_confine(plan)):
                    pass
            finally:
                restore_worktree(integ, snap)
    except UIUnavailable as e:
        return f"the dev command does not bring the app up before any change: {e}"
    why = check_chrome()
    if why:
        return f"Chrome can't take screenshots here: {why}"
    return None


# ===================================================================== cli ===
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chrome", help="print the Chrome/Chromium binary that would be used")
    c = sub.add_parser("capture", help="screenshot pages of a running app (or start one with --dev)")
    c.add_argument("--url", required=True)
    c.add_argument("--page", action="append", help="app path like /share/demo, or a JSON page (repeat)")
    c.add_argument("--viewport", action="append", choices=list(VIEWPORTS))
    c.add_argument("--out", required=True)
    c.add_argument("--dev", help="command that serves the app; started and stopped around the capture")
    c.add_argument("--cwd", default=".")
    c.add_argument("--ready-timeout", type=float, default=DEFAULT_READY_TIMEOUT_S)
    a = ap.parse_args(argv)
    if a.cmd == "chrome":
        b = find_chrome()
        print(b or chrome_missing())
        return 0 if b else 1
    pages = [json.loads(p) if p.strip().startswith("{") else {"path": p} for p in (a.page or ["/"])]
    errs = validate_pages(pages, "--page")
    if errs:
        print("\n".join(errs), file=sys.stderr)
        return 2
    plan = {"ui": {"pages": pages}}
    out = Path(a.out).resolve()
    failures: list = []
    try:
        with signals_raise():
            if a.dev:
                with dev_server(a.dev, a.url, Path(a.cwd).resolve(), out / "_devserver.log", a.ready_timeout) as p:
                    shots = shoot(a.url, collect_pages(plan), a.viewport or list(VIEWPORTS), out, failures=failures,
                                  alive=lambda: check_alive(p, a.url, out / "_devserver.log"))
            else:
                shots = shoot(a.url, collect_pages(plan), a.viewport or list(VIEWPORTS), out, failures=failures)
    except UIUnavailable as e:
        print(f"UI capture could not run: {e}", file=sys.stderr)
        return 3
    print(json.dumps({"dir": str(out), "shots": shots, "page_failures": failures}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
