#!/usr/bin/env python3
"""Tests for ui_review.py: websocket framing, the dev-server lifecycle and its fail-closed paths,
and (when Chrome is installed) a real headless-Chrome capture of a small static site.

Run from the repo root: python3 tests/test_ui_review.py -v
"""
import base64
import hashlib
import http.server
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "orchestrate" / "scripts"
sys.path.insert(0, str(SCRIPTS))
_STATE = tempfile.mkdtemp(prefix="ui-review-test-state-")
os.environ["ORCHESTRATE_DIR"] = _STATE  # the dev-server lock lives here, not in the user's state
os.environ["ORCHESTRATE_DASHBOARD"] = "0"
_USER_CHROME = os.environ.pop("ORCHESTRATE_CHROME", None)

import ui_review as U  # noqa: E402

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def png_size(p: Path) -> tuple:
    b = p.read_bytes()[:24]
    assert b[:8] == PNG_SIG, p
    return struct.unpack(">II", b[16:24])


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie is dead for our purposes
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


class ProcStatWithoutProcTest(unittest.TestCase):
    def test_without_proc_the_session_comes_from_getsid_not_from_ps(self):
        # macOS has no /proc, and its `ps -o sess=` doesn't print a session id
        def ps(cmd, **kw):
            out = "100 200" + (" c0dedbad" if "sess" in cmd[2] else "")
            return U.subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

        with mock.patch.object(U.Path, "read_text", side_effect=OSError), \
                mock.patch.object(U.subprocess, "run", ps), mock.patch.object(U.os, "getsid", return_value=300):
            self.assertEqual(U._proc_stat(4242), (100, 200, 300))


class FramingTest(unittest.TestCase):
    def test_lengths_round_trip_masked_and_unmasked(self):
        for n in (0, 1, 125, 126, 127, 65535, 65536, 3 * 1024 * 1024 + 7):
            payload = bytes(range(256)) * (n // 256) + bytes(range(n % 256))
            for mask in (True, False):
                f = U.encode_frame(U.OP_TEXT, payload, mask=mask)
                hdr = 2 + (0 if n < 126 else 2 if n < 65536 else 8) + (4 if mask else 0)
                self.assertEqual(len(f), hdr + n)
                self.assertEqual(bool(f[1] & 0x80), mask, "client frames are masked")
                self.assertEqual(f[1] & 0x7F, n if n < 126 else 126 if n < 65536 else 127)
                fin, op, got, used = U.parse_frame(f + b"extra")
                self.assertEqual((fin, op, got, used), (True, U.OP_TEXT, payload, len(f)))

    def test_masking_is_rfc6455(self):
        # RFC 6455 section 5.7: a masked "Hello" with key 37 fa 21 3d
        f = U.encode_frame(U.OP_TEXT, b"Hello", mask_key=bytes.fromhex("37fa213d"))
        self.assertEqual(f, bytes.fromhex("818537fa213d7f9f4d5158"))
        self.assertEqual(U.parse_frame(bytes.fromhex("810548656c6c6f"))[:3], (True, U.OP_TEXT, b"Hello"))

    def test_partial_frames_consume_nothing(self):
        f = U.encode_frame(U.OP_TEXT, b"x" * 70000, mask=False)
        for cut in (0, 1, 2, 5, 9, 10, 100, len(f) - 1):
            self.assertIsNone(U.parse_frame(f[:cut]), cut)
        m = U.encode_frame(U.OP_TEXT, b"abc", mask=True)
        for cut in range(len(m)):
            self.assertIsNone(U.parse_frame(m[:cut]))

    def test_fin_and_continuation(self):
        a = U.encode_frame(U.OP_TEXT, b"he", fin=False, mask=False)
        b = U.encode_frame(U.OP_CONT, b"llo", mask=False)
        self.assertEqual(U.parse_frame(a)[:3], (False, U.OP_TEXT, b"he"))
        self.assertEqual(U.parse_frame(b)[:3], (True, U.OP_CONT, b"llo"))


class WebSocketServerTest(unittest.TestCase):
    """The client against a tiny server: handshake, ping, fragmented and 64-bit-length messages
    split across TCP writes, client masking, close."""

    def test_client_against_a_live_server(self):
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        port = lsock.getsockname()[1]
        got = {}
        big = "B" * (2 * 1024 * 1024 + 3)

        def serve():
            c, _ = lsock.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                data += c.recv(4096)
            key = [ln.split(":", 1)[1].strip() for ln in data.decode().split("\r\n")
                   if ln.lower().startswith("sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1((key + U.WS_GUID).encode()).digest()).decode()
            c.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {acc}\r\n\r\n".encode())
            out = (U.encode_frame(U.OP_PING, b"p1", mask=False)
                   + U.encode_frame(U.OP_TEXT, b'{"a":', fin=False, mask=False)
                   + U.encode_frame(U.OP_PING, b"p2", mask=False)        # control frame mid-message
                   + U.encode_frame(U.OP_CONT, b' "frag', fin=False, mask=False)
                   + U.encode_frame(U.OP_CONT, b'mented"}', mask=False)
                   + U.encode_frame(U.OP_TEXT, big.encode(), mask=False))
            for i in range(0, len(out), 7001):  # dribble it out in odd-sized writes
                c.sendall(out[i:i + 7001])
            buf = bytearray()
            frames = []
            masks = []
            c.settimeout(10)
            while len(frames) < 3:
                f = U.parse_frame(buf)
                if f:
                    masks.append(bool(buf[1] & 0x80))  # the MASK bit of the frame just parsed
                    del buf[:f[3]]
                    frames.append(f)
                    continue
                buf += c.recv(65536)
            got["frames"] = frames
            got["masks"] = masks
            c.sendall(U.encode_frame(U.OP_CLOSE, struct.pack(">H", 1000), mask=False))
            time.sleep(0.2)
            c.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        ws = U.WebSocket.connect(f"ws://127.0.0.1:{port}/devtools/browser/x")
        self.assertEqual(ws.recv(10), '{"a": "fragmented"}')
        self.assertEqual(ws.recv(10), big)
        ws.send_text("hi there")
        with self.assertRaises(ConnectionError):
            ws.recv(10)
        t.join(10)
        lsock.close()
        ws.close()
        pongs = [(op, p) for _, op, p, _ in got["frames"][:2]]
        self.assertEqual(pongs, [(U.OP_PONG, b"p1"), (U.OP_PONG, b"p2")])
        self.assertEqual(got["frames"][2][1:3], (U.OP_TEXT, b"hi there"))
        self.assertEqual(got["masks"], [True, True, True], "every client frame is masked (RFC 6455 5.3)")

    def test_timeout_leaves_the_stream_intact(self):
        a, b = socket.socketpair()
        ws = U.WebSocket(a)
        f = U.encode_frame(U.OP_TEXT, b"hello world", mask=False)
        b.sendall(f[:5])
        with self.assertRaises(TimeoutError):
            ws.recv(0.2)
        b.sendall(f[5:])
        self.assertEqual(ws.recv(2), "hello world")
        a.close()
        b.close()


class StaticServer:
    """`python3 -m http.server`-like server in a thread, for "something already serves the URL"."""

    def __init__(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        self.srv.RequestHandlerClass.log_message = lambda *a: None
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class DevServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ui-dev-"))
        self.lock = self.tmp / "devserver.lock"
        self.log = self.tmp / "logs" / "_devserver.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refuses_when_something_already_serves_the_url(self):
        s = StaticServer()
        try:
            with self.assertRaises(U.UIUnavailable) as cm:
                with U.dev_server(f"touch {self.tmp / 'ran'}", s.url, self.tmp, self.log, 5, lock_path=self.lock):
                    pass
        finally:
            s.stop()
        self.assertIn("already serving", str(cm.exception))
        self.assertFalse((self.tmp / "ran").exists(), "the dev command never started")

    def test_not_ready_in_time_kills_the_whole_group(self):
        cmd = f"echo $$ > {self.tmp / 'sh.pid'}; sleep 60 & echo $! > {self.tmp / 'child.pid'}; wait"
        url = f"http://127.0.0.1:{free_port()}"
        t0 = time.time()
        with self.assertRaises(U.UIUnavailable) as cm:
            with U.dev_server(cmd, url, self.tmp, self.log, 1.5, lock_path=self.lock):
                pass
        self.assertLess(time.time() - t0, 15)
        self.assertIn("not ready", str(cm.exception))
        for f in ("sh.pid", "child.pid"):
            pid = int((self.tmp / f).read_text())
            self.assertFalse(pid_alive(pid), f"{f} {pid} survived")
        self.assertIn("$ echo $$", self.log.read_text())

    def test_dev_command_that_exits_early(self):
        with self.assertRaises(U.UIUnavailable) as cm:
            with U.dev_server("echo nope >&2; exit 7", f"http://127.0.0.1:{free_port()}", self.tmp, self.log, 10,
                              lock_path=self.lock):
                pass
        self.assertIn("exited with code 7", str(cm.exception))
        self.assertIn("nope", str(cm.exception))

    def test_server_is_killed_after_the_block_even_on_error(self):
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        with self.assertRaises(RuntimeError):
            with U.dev_server(f"{sys.executable} -m http.server {port} --bind 127.0.0.1", url, self.tmp,
                              self.log, 20, lock_path=self.lock):
                self.assertTrue(U.url_answers(url))
                raise RuntimeError("boom inside")
        self.assertFalse(U.url_answers(url))

    def test_one_dev_server_at_a_time(self):
        import fcntl
        with open(self.lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaises(U.UIUnavailable) as cm:
                with U.dev_server("true", f"http://127.0.0.1:{free_port()}", self.tmp, self.log, 5,
                                  lock_path=self.lock, lock_wait=1):
                    pass
        self.assertIn("held", str(cm.exception))


class DevServerHardeningTest(unittest.TestCase):
    """The dev server never outlives the run (SIGTERM, SIGKILL), a port held by a slow server is
    still seen as taken, and whatever the dev server writes in the worktree is put back."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ui-hard-"))
        self.lock = self.tmp / "devserver.lock"
        self.log = self.tmp / "logs" / "_devserver.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_until(self, cond, secs=20.0):
        end = time.time() + secs
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        return False

    def test_a_port_held_by_a_slow_server_counts_as_taken(self):
        # accepts TCP, never answers HTTP: the old 2 s HTTP probe saw "nothing there"
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(5)
        url = f"http://127.0.0.1:{ls.getsockname()[1]}"
        try:
            self.assertFalse(U.url_answers(url, timeout=0.5))
            self.assertTrue(U.port_taken(url))
            with self.assertRaises(U.UIUnavailable) as cm:
                with U.dev_server(f"touch {self.tmp / 'ran'}", url, self.tmp, self.log, 5, lock_path=self.lock):
                    pass
        finally:
            ls.close()
        self.assertIn("already serving", str(cm.exception))
        self.assertFalse((self.tmp / "ran").exists())
        self.assertIsNone(U.port_taken(f"http://127.0.0.1:{free_port()}"))

    def test_sigterm_mid_capture_stops_the_dev_server_and_chrome(self):
        import signal
        import subprocess
        if not real_chrome():
            self.skipTest("no Chrome")
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        site = self.tmp / "site"
        site.mkdir()
        (site / "index.html").write_text("<title>t</title>hi")
        env = {**os.environ, "ORCHESTRATE_DIR": str(self.tmp / "state")}
        p = subprocess.Popen([sys.executable, str(SCRIPTS / "ui_review.py"), "capture", "--url", url, "--page",
                              json.dumps({"path": "/", "steps": [{"wait_ms": 30000}]}), "--viewport", "desktop",
                              "--out", str(self.tmp / "out"), "--dev",
                              f"exec {sys.executable} -m http.server {port} --bind 127.0.0.1", "--cwd", str(site)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            self.assertTrue(self.wait_until(lambda: (self.tmp / "out" / "_devserver.log").exists()
                                            and U.url_answers(url)), "the dev server came up")
            reg = self.tmp / "state" / "devserver.pids"
            self.assertTrue(self.wait_until(lambda: "chrome" in (reg.read_text() if reg.exists() else "")),
                            "Chrome is recorded while it runs")
            pids = [e["pid"] for e in json.loads(reg.read_text())]
            time.sleep(1)
            p.send_signal(signal.SIGTERM)
            p.wait(30)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertFalse(U.url_answers(url), "the dev server was stopped on SIGTERM")
        self.assertIsNone(U.port_taken(url))
        for pid in pids:
            self.assertFalse(pid_alive(pid), f"process group {pid} survived SIGTERM")
        self.assertFalse(reg.exists() and json.loads(reg.read_text()), "nothing left recorded")

    def test_a_sigkilled_run_is_reaped_by_the_next_one(self):
        import subprocess
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(SCRIPTS)!r})
            import ui_review as U
            from pathlib import Path
            with U.dev_server({f"exec {sys.executable} -m http.server {port} --bind 127.0.0.1"!r}, {url!r},
                              Path({str(self.tmp)!r}), Path({str(self.log)!r}), 20, lock_path=Path({str(self.lock)!r})):
                print("up", flush=True)
                time.sleep(60)
        """)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "up")
        finally:
            p.kill()  # SIGKILL: no finally runs
            p.wait(10)
            p.stdout.close()
        self.assertTrue(U.url_answers(url), "the orphaned dev server still holds the port")
        with U.dev_server(f"exec {sys.executable} -m http.server {port} --bind 127.0.0.1", url, self.tmp,
                          self.log, 20, lock_path=self.lock):
            self.assertTrue(U.url_answers(url))
        self.assertFalse(U.url_answers(url))
        self.assertFalse(self.lock.with_suffix(".pids").exists())

    def test_a_reused_pid_is_never_killed(self):
        import subprocess
        bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            reg = self.tmp / "devserver.pids"
            reg.write_text(json.dumps([{"pid": bystander.pid, "what": "dev server", "started": "Mon Jan  1 00:00:00 2001"}]))
            self.assertEqual(U.reap_stale(reg), [])
            self.assertIsNone(bystander.poll(), "a process whose start time differs is left alone")
        finally:
            bystander.kill()
            bystander.wait()

    def test_validate_ui_rejects_malformed_values_without_crashing(self):
        base = {"dev": "d", "url": "http://127.0.0.1:5173"}
        self.assertEqual(U.validate_ui(base), [])
        self.assertEqual(U.validate_ui({**base, "url": "https://localhost:5173/app"}), [])
        for over, needle in [({"viewports": [["a"]]}, "viewports"), ({"viewports": [{"x": 1}]}, "viewports"),
                             ({"url": "http://127.0.0.1:5173/?a=1"}, "query"),
                             ({"url": "http://127.0.0.1:5173/#x"}, "fragment"),
                             ({"url": "http://127.0.0.1:5173 "}, "spaces"),
                             ({"url": "http://h:99999"}, "invalid port"),
                             ({"ready_timeout_s": 0.5}, "ready_timeout_s")]:
            errs = U.validate_ui({**base, **over})
            self.assertTrue(any(needle in e for e in errs), (over, errs))


class WorktreeRestoreTest(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.tmp = Path(tempfile.mkdtemp(prefix="ui-restore-"))
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        (self.wt / "index.html").write_text("<title>x</title>hi")
        (self.wt / "tsconfig.json").write_text("{}\n")
        (self.wt / "wip.txt").write_text("base\n")
        subprocess.run("git init -q -b main && git add -A && git -c user.email=t@t -c user.name=t commit -qm init",
                       shell=True, cwd=self.wt, check=True)
        (self.wt / "wip.txt").write_text("uncommitted work\n")  # changed before the dev server starts
        self.job = SimpleNamespace(dir=self.tmp / "job", state={"integration_wt": str(self.wt)})

    def tearDown(self):
        os.environ.pop("ORCHESTRATE_CHROME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self):
        import subprocess
        return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.wt,
                              capture_output=True, text=True).stdout

    def test_what_the_dev_server_writes_is_put_back(self):
        fake = self.tmp / "chrome"
        fake.write_text("#!/bin/sh\nexit 3\n")
        fake.chmod(0o755)
        os.environ["ORCHESTRATE_CHROME"] = str(fake)  # the capture fails: the worktree is put back anyway
        port = free_port()
        dev = (f"echo cache > dev-artifact.db; mkdir -p .devcache/x && echo y > .devcache/x/chunk.js; "
               f"echo '{{\"changed\": 1}}' > tsconfig.json; echo clobbered > wip.txt; rm index.html; "
               f"exec {sys.executable} -m http.server {port} --bind 127.0.0.1")
        before = self.status()
        res = U.capture({"ui": {"dev": dev, "url": f"http://127.0.0.1:{port}", "ready_timeout_s": 20}, "tasks": []},
                        self.job, 1)
        self.assertIn("Chrome exited with code 3", res["error"])
        self.assertEqual(self.status(), before, "only the change made before the capture is left")
        self.assertEqual((self.wt / "wip.txt").read_text(), "uncommitted work\n")
        self.assertEqual((self.wt / "tsconfig.json").read_text(), "{}\n")
        self.assertTrue((self.wt / "index.html").exists())
        self.assertFalse((self.wt / ".devcache").exists())
        self.assertEqual(sorted(res["restored"]), sorted([".devcache/x/chunk.js", "dev-artifact.db", "index.html",
                                                          "tsconfig.json", "wip.txt"]))
        self.assertTrue((self.wt / ".orchestrate" / "screenshots" / "round-1").is_dir(), "screenshots are kept")

    def test_preflight_checks_the_dev_command_and_puts_the_worktree_back(self):
        port = free_port()
        ok = {"ui": {"dev": f"echo x > junk.txt; exec {sys.executable} -m http.server {port} --bind 127.0.0.1",
                     "url": f"http://127.0.0.1:{port}", "ready_timeout_s": 20}}
        if real_chrome():  # the preflight also starts Chrome once
            self.assertIsNone(U.preflight(ok, self.job))
        else:
            self.assertIn("Chrome can't take screenshots here", U.preflight(ok, self.job))
        self.assertFalse((self.wt / "junk.txt").exists())
        self.assertFalse(U.url_answers(ok["ui"]["url"]))
        bad = {"ui": {"dev": "echo 'command not found: pnpm' >&2; exit 127", "url": f"http://127.0.0.1:{port}",
                      "ready_timeout_s": 20}}
        why = U.preflight(bad, self.job)
        self.assertIn("does not bring the app up", why)
        self.assertIn("command not found", why)


class CaptureFailClosedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ui-cap-"))
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        (self.wt / "index.html").write_text("<title>x</title>hi")
        self.job = SimpleNamespace(dir=self.tmp / "job", state={"integration_wt": str(self.wt)})

    def tearDown(self):
        os.environ.pop("ORCHESTRATE_CHROME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, port):
        return {"ui": {"dev": f"{sys.executable} -m http.server {port} --bind 127.0.0.1",
                       "url": f"http://127.0.0.1:{port}", "ready_timeout_s": 20}, "tasks": []}

    def test_explicit_chrome_setting_is_authoritative(self):
        os.environ["ORCHESTRATE_CHROME"] = str(self.tmp / "nope")
        self.assertIsNone(U.find_chrome())
        port = free_port()
        res = U.capture(self.plan(port), self.job, 1)
        self.assertFalse(res["ran"])
        self.assertIn("no Chrome or Chromium found", res["error"])
        # it says what was set and that nothing else was tried, not that it searched PATH
        self.assertIn(f"$ORCHESTRATE_CHROME is set to {str(self.tmp / 'nope')!r}", res["error"])
        self.assertIn("nothing else is tried", res["error"])
        self.assertNotIn("google-chrome-stable", res["error"])
        self.assertFalse(U.url_answers(f"http://127.0.0.1:{port}"), "no dev server was left running")
        os.environ.pop("ORCHESTRATE_CHROME")
        self.assertIn("google-chrome-stable", U.chrome_missing(), "unset, it names every place it looks")

    def test_chrome_that_crashes_fails_closed_and_stops_the_dev_server(self):
        fake = self.tmp / "chrome"
        fake.write_text("#!/bin/sh\necho 'fake chrome: cannot open display' >&2\nexit 3\n")
        fake.chmod(0o755)
        os.environ["ORCHESTRATE_CHROME"] = str(fake)
        port = free_port()
        res = U.capture(self.plan(port), self.job, 1)
        self.assertFalse(res["ran"])
        self.assertIn("Chrome exited with code 3", res["error"])
        self.assertIn("cannot open display", res["error"])
        self.assertFalse(U.url_answers(f"http://127.0.0.1:{port}"))
        self.assertEqual((self.wt / ".orchestrate" / ".gitignore").read_text(), "*\n")
        self.assertTrue((self.tmp / "job/logs/_devserver.log").exists())
        self.assertEqual([p for p in Path(tempfile.gettempdir()).glob("orchestrate-chrome-*")
                          if p.stat().st_mtime > time.time() - 30 and (p / "chrome.log").exists()
                          and "cannot open display" in (p / "chrome.log").read_text()], [],
                         "the temp profile is removed")

    def test_preflight_fails_when_chrome_cannot_start_and_leaves_no_chrome_temp_dirs(self):
        fake = self.tmp / "chrome"
        fake.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import os, sys, tempfile
            prof = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--user-data-dir="))
            d = tempfile.mkdtemp(prefix="com.google.Chrome.", dir={str(Path(tempfile.gettempdir()))!r})
            open(os.path.join({str(self.tmp)!r}, "scratch"), "w").write(d)
            open(os.path.join(d, "SingletonSocket"), "w").close()
            os.symlink(os.path.join(d, "SingletonSocket"), os.path.join(prof, "SingletonSocket"))
            sys.stderr.write("fake chrome: crashed on start\\n")
            sys.exit(5)
            """))
        fake.chmod(0o755)
        os.environ["ORCHESTRATE_CHROME"] = str(fake)
        port = free_port()
        why = U.preflight(self.plan(port), self.job)
        self.assertIn("Chrome can't take screenshots here: Chrome exited with code 5", why)
        self.assertIn("crashed on start", why)
        scratch = Path((self.tmp / "scratch").read_text())
        self.assertTrue(scratch.name.startswith("com.google.Chrome."))
        self.assertFalse(scratch.exists(), "Chrome's scratch dir is removed after a crash")
        self.assertFalse(U.url_answers(f"http://127.0.0.1:{port}"), "the dev server was stopped")
        # a scratch dir that is not Chrome's, or not in the temp dir, is never removed
        prof = self.tmp / "prof"
        prof.mkdir()
        other = self.tmp / "com.google.Chrome.keep"
        other.mkdir()
        (prof / "SingletonSocket").symlink_to(other / "SingletonSocket")
        U.remove_profile(str(prof))
        self.assertFalse(prof.exists())
        self.assertTrue(other.exists())

    def test_check_chrome_reports_a_chrome_that_will_not_start(self):
        fake = self.tmp / "chrome"
        fake.write_text("#!/bin/sh\necho 'no display' >&2\nexit 9\n")
        fake.chmod(0o755)
        why = U.check_chrome(str(fake))
        self.assertIn("Chrome exited with code 9", why)
        self.assertIn("no display", why)


SITE = textwrap.dedent("""\
    <!doctype html><html><head><title>Demo Home</title>
    <meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body><h1 id="h">Hello</h1>
    <button id="go" onclick="document.getElementById('h').textContent='Clicked';
      var d=document.createElement('div');d.id='done';d.textContent='done';document.body.appendChild(d)">Go</button>
    <input id="q">
    <div style="height:1500px;background:linear-gradient(red,blue)"></div>
    <script>console.error("boom from page"); setTimeout(function(){ throw new Error("late kaboom") }, 10);</script>
    </body></html>
""")


def real_chrome():
    os.environ.pop("ORCHESTRATE_CHROME", None)
    if _USER_CHROME:
        os.environ["ORCHESTRATE_CHROME"] = _USER_CHROME
    return U.find_chrome()


@unittest.skipUnless(real_chrome(), "no Chrome/Chromium installed")
class RealChromeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ui-real-"))
        os.environ["ORCHESTRATE_SANDBOX_WRITE"] = str(self.tmp)  # the fake servers' scripts live here
        self.addCleanup(os.environ.pop, "ORCHESTRATE_SANDBOX_WRITE", None)
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        (self.wt / "index.html").write_text(SITE)
        self.job = SimpleNamespace(dir=self.tmp / "job", state={"integration_wt": str(self.wt)})
        self.old_wait = U.WAIT_FOR_S
        U.WAIT_FOR_S = 1.5

    def tearDown(self):
        U.WAIT_FOR_S = self.old_wait
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_chrome_takes_a_screenshot_and_cleans_up(self):
        self.assertIsNone(U.check_chrome())
        br = U.Chrome().__enter__()
        scratch = U._chrome_scratch(br.tmp)
        prof = br.tmp
        self.assertIsNotNone(scratch, "Chrome links its SingletonSocket from a temp dir")
        os.killpg(br.proc.pid, 9)  # a crashed Chrome can't clean up after itself
        br.proc.wait(10)
        br.close()
        self.assertFalse(Path(prof).exists())
        self.assertFalse(scratch.exists())

    def test_capture_desktop_mobile_and_a_click_flow(self):
        port = free_port()
        plan = {"ui": {"dev": f"{sys.executable} -m http.server {port} --bind 127.0.0.1",
                       "url": f"http://127.0.0.1:{port}", "ready_timeout_s": 30,
                       "pages": [{"path": "/", "name": "home", "steps": [
                           {"type": ["#q", "hello"]}, {"click": "#go"}, {"wait_for": "#done"},
                           {"screenshot": "after-click"}, {"wait_for": "#never-appears"}]}]},
                "tasks": [{"id": "web", "ui": [{"path": "/missing"}]}]}
        res = U.capture(plan, self.job, 1)
        self.assertIsNone(res["error"], res["error"])
        self.assertTrue(res["ran"])
        self.assertFalse(U.url_answers(plan["ui"]["url"]), "the dev server was stopped")
        shots = {s["id"]: s for s in res["shots"]}
        self.assertEqual(sorted(shots), sorted([
            "round-1/home--desktop--after-click", "round-1/home--desktop",
            "round-1/home--mobile--after-click", "round-1/home--mobile",
            "round-1/missing--desktop", "round-1/missing--mobile"]))
        root = self.wt / ".orchestrate" / "screenshots"
        for s in res["shots"]:
            w, h = png_size(root / s["file"])
            self.assertEqual(w, 1440 if s["viewport"] == "desktop" else 780, s["id"])  # mobile: 390 at DPR 2
            self.assertGreaterEqual(h, 900 if s["viewport"] == "desktop" else 1688)
        home = shots["round-1/home--desktop"]
        self.assertEqual((home["status"], home["title"], home["step"], home["tasks"]), (200, "Demo Home", None, []))
        self.assertTrue(any("boom from page" in c for c in home["console"]), home["console"])
        self.assertTrue(any("late kaboom" in e for e in home["page_errors"]), home["page_errors"])
        self.assertTrue(any("#never-appears" in e for e in home["step_errors"]), home["step_errors"])
        self.assertEqual(len(home["step_errors"]), 1, "the failing step was the last: nothing was skipped")
        for s in res["shots"]:  # the browser's own /favicon.ico fetch is noise, never evidence
            self.assertFalse([m for m in s["console"] + s["failed_requests"] if "favicon.ico" in m], s["id"])
        mid = shots["round-1/home--mobile--after-click"]
        self.assertEqual(mid["step"], "after-click")
        self.assertEqual(mid["step_errors"], [], "the failing step came after this shot")
        self.assertGreater(png_size(root / mid["file"])[1], 1688, "full page, beyond the viewport")
        missing = shots["round-1/missing--mobile"]
        self.assertEqual((missing["status"], missing["tasks"]), (404, ["web"]))
        self.assertEqual((self.wt / ".orchestrate" / ".gitignore").read_text(), "*\n")
        # the click really happened: the after-click shot differs from nothing-clicked state
        self.assertNotEqual((root / "round-1/home--desktop--after-click.png").read_bytes(),
                            (root / "round-1/missing--desktop.png").read_bytes())

    def test_dialogs_hung_pages_tall_pages_and_pages_without_a_viewport_meta(self):
        meta = '<meta name="viewport" content="width=device-width,initial-scale=1">'
        (self.wt / "dialog.html").write_text(
            f"<title>d</title>{meta}<button id=del onclick=\"if(confirm('Delete it?')){{var d=document.createElement('p');"
            f"d.id='yes';d.textContent='deleted';document.body.appendChild(d)}}\">Delete</button>")
        (self.wt / "alert.html").write_text(f"<title>a</title>{meta}<script>alert('welcome')</script><h1 id=after>after</h1>")
        (self.wt / "loop.html").write_text(f"<title>l</title>{meta}<button id=b onclick=\"while(true){{}}\">hang</button>")
        (self.wt / "tall.html").write_text(f"<title>t</title>{meta}<div style='height:3000px;background:linear-gradient(red,blue)'></div>")
        (self.wt / "novp.html").write_text("<title>n</title><div style='display:flex;justify-content:space-between;width:960px'>"
                                           "<b>LEFT</b><b>MIDDLE</b><b>RIGHT-EDGE</b></div>")
        port = free_port()
        plan = {"ui": {"dev": f"{sys.executable} -m http.server {port} --bind 127.0.0.1",
                       "url": f"http://127.0.0.1:{port}", "ready_timeout_s": 30, "viewports": ["mobile"],
                       "pages": [{"path": "/dialog.html", "steps": [{"click": "#del"}, {"wait_for": "#yes"}]},
                                 {"path": "/alert.html", "steps": [{"wait_for": "#after"}]},
                                 {"path": "/loop.html", "steps": [{"click": "#b"}]},
                                 {"path": "/tall.html"}, {"path": "/novp.html"}]}, "tasks": []}
        t0 = time.time()
        res = U.capture(plan, self.job, 1)
        self.assertIsNone(res["error"], res["error"])
        self.assertLess(time.time() - t0, 90)
        shots = {s["name"]: s for s in res["shots"]}
        self.assertEqual(sorted(shots), ["alert-html", "dialog-html", "novp-html", "tall-html"])
        d = shots["dialog-html"]
        self.assertEqual(d["step_errors"], [], "the confirm was accepted, so #yes appeared")
        self.assertTrue(any("[dialog confirm] Delete it?" in c for c in d["console"]), d["console"])
        a = shots["alert-html"]
        self.assertEqual((a["status"], a["step_errors"]), (200, []))
        self.assertTrue(any("[dialog alert] welcome" in c for c in a["console"]), a["console"])
        # the hung page is a failure of that page, not of the whole capture
        self.assertEqual(len(res["page_failures"]), 1, res["page_failures"])
        f = res["page_failures"][0]
        self.assertEqual((f["name"], f["viewport"], f["step"]), ("loop-html", "mobile", 1))
        self.assertIn("stopped responding at step 1", f["error"])
        root = self.wt / ".orchestrate" / "screenshots"
        # a tall page also comes as viewport-sized tiles a model can read
        t = shots["tall-html"]
        self.assertGreaterEqual(len(t["tiles"]), 3, t)
        for tile in t["tiles"]:
            w, h = png_size(root / tile)
            self.assertEqual(w, 780)
            self.assertLessEqual(h, 1688)
        self.assertEqual(shots["dialog-html"]["tiles"], [], "a short page has no tiles")
        # no viewport meta: the phone shows the 980 px layout zoomed out, and the shot says so
        n = shots["novp-html"]
        self.assertTrue(any("laid out 980" in x for x in n["notes"]), n["notes"])
        w, h = png_size(root / n["file"])
        self.assertEqual(w, 780)
        self.assertLess(h, 2400, "the zoomed-out page is not padded with the 980-wide layout's blank height")

    def test_dev_server_that_dies_mid_capture_fails_closed(self):
        port = free_port()
        server = self.tmp / "dies.py"  # ends itself: macOS has no `timeout` command
        server.write_text("import http.server, os, threading\n"
                          "threading.Timer(4, os._exit, (124,)).start()\n"
                          f"http.server.ThreadingHTTPServer(('127.0.0.1', {port}), "
                          "http.server.SimpleHTTPRequestHandler).serve_forever()\n")
        plan = {"ui": {"dev": f"exec {sys.executable} {server}",
                       "url": f"http://127.0.0.1:{port}", "ready_timeout_s": 20, "viewports": ["desktop"],
                       "pages": [{"path": "/", "steps": [{"wait_ms": 5000}]}, {"path": "/index.html"}]},
                "tasks": []}
        res = U.capture(plan, self.job, 1)
        self.assertFalse(res["ran"])
        self.assertFalse(res["app_failed"])
        self.assertIn("exited with code 124 during the capture", res["error"])

    def test_cli_capture(self):
        import subprocess
        port = free_port()
        out = self.tmp / "out"
        p = subprocess.run([sys.executable, str(SCRIPTS / "ui_review.py"), "capture", "--url",
                            f"http://127.0.0.1:{port}", "--page", "/", "--viewport", "desktop", "--out", str(out),
                            "--dev", f"{sys.executable} -m http.server {port} --bind 127.0.0.1", "--cwd", str(self.wt)],
                           capture_output=True, text=True, timeout=120, env=os.environ)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        data = json.loads(p.stdout)
        self.assertEqual([s["id"] for s in data["shots"]], ["home--desktop"])
        self.assertEqual(png_size(out / "home--desktop.png")[0], 1440)


class ShotNameTest(unittest.TestCase):
    def test_names_cannot_contain_the_shot_id_separator(self):
        # "home--desktop" at mobile and "home" + screenshot "mobile" at desktop would share one id and PNG
        clash = {"dev": "d", "url": "http://127.0.0.1:5173", "pages": [
            {"path": "/a", "name": "home--desktop"},
            {"path": "/", "name": "home", "steps": [{"screenshot": "mobile"}]}]}
        errs = U.validate_ui(clash)
        self.assertTrue(any("plan.ui.pages[0].name" in e for e in errs), errs)
        for bad in ("a--b", "-a", "a-", "A", "a_b", "x" * 42):
            self.assertTrue(U.validate_pages([{"path": "/", "name": bad}], "p"), bad)
            self.assertTrue(U.validate_pages([{"path": "/", "steps": [{"screenshot": bad}]}], "p"), bad)
        for good in ("home", "home-2", "a1-b2-c3", "x" * 41):
            self.assertEqual(U.validate_pages([{"path": "/", "name": good, "steps": [{"screenshot": good}]}], "p"),
                             [], good)


class FakeCDP(U.CDP):
    """A CDP whose commands all succeed at once; events are fed in with _event()."""

    def __init__(self):
        super().__init__(None)

    def call(self, method, params=None, session=None, timeout=30.0):
        return {"browserContextId": "CTX", "targetId": "MAIN", "sessionId": "S1"}


def ev(method, **params):
    return {"method": method, "sessionId": "S1", "params": params}


class TabEventsTest(unittest.TestCase):
    def test_status_is_the_document_on_screen_not_the_first_one(self):
        cdp = FakeCDP()
        tab = U.Tab(cdp, "desktop")
        tab.loader = "L1"
        for m in (ev("Network.requestWillBeSent", requestId="L1", type="Document", frameId="MAIN",
                     request={"url": "http://x/"}),
                  ev("Network.responseReceived", requestId="L1", type="Document", frameId="MAIN",
                     response={"status": 200, "url": "http://x/"})):
            cdp._event(m)
        tab.process()
        self.assertEqual((tab.status, tab.first_status), (200, 200))
        # an iframe's 404 document is a failed request, not the page's status
        for m in (ev("Network.requestWillBeSent", requestId="F1", type="Document", frameId="CHILD",
                     request={"url": "http://x/frame"}),
                  ev("Network.responseReceived", requestId="F1", type="Document", frameId="CHILD",
                     response={"status": 404, "url": "http://x/frame"})):
            cdp._event(m)
        tab.process()
        self.assertEqual(tab.status, 200)
        self.assertIn("HTTP 404 http://x/frame", tab.failed)
        # a JS redirect or a clicked link loads a new main-frame document that answers 500
        for m in (ev("Network.requestWillBeSent", requestId="D2", type="Document", frameId="MAIN",
                     request={"url": "http://x/boom"}),
                  ev("Network.responseReceived", requestId="D2", type="Document", frameId="MAIN",
                     response={"status": 500, "url": "http://x/boom"})):
            cdp._event(m)
        tab.process()
        self.assertEqual((tab.status, tab.first_status), (500, 200))

    def test_a_page_that_logs_in_a_loop_cannot_grow_the_event_queue(self):
        cdp = FakeCDP()
        tab = U.Tab(cdp, "desktop")
        n = U.CONSOLE_QUEUE_MAX * 20
        for i in range(n):
            cdp._event(ev("Runtime.consoleAPICalled", type="log", args=[{"value": f"x{i}"}]))
        cdp._event(ev("Page.loadEventFired"))  # other events are still queued
        self.assertEqual(len(cdp.events), U.CONSOLE_QUEUE_MAX + 1)
        tab.process()
        self.assertTrue(tab.loaded)
        self.assertEqual(tab.dropped, n - U.CONSOLE_QUEUE_MAX)
        cdp._event(ev("Runtime.consoleAPICalled", type="error", args=[{"value": "after the read"}]))
        tab.process()
        self.assertIn("[error] after the read", tab.console, "the cap is per read, not for good")


class ForeignListenerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ui-foreign-"))
        self.lock = self.tmp / "devserver.lock"
        self.log = self.tmp / "logs" / "_devserver.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_server_that_takes_the_port_after_the_check_is_refused(self):
        if not (Path("/proc/net/tcp").exists() or shutil.which("lsof")):
            self.skipTest("this system can't tell who listens on a port")
        port, other = free_port(), free_port()
        url = f"http://127.0.0.1:{port}"
        # the dev command stays alive but serves another port (as vite and next do when theirs is taken)
        cmd = f"echo $$ > {self.tmp / 'dev.pid'}; sleep 3; exec {sys.executable} -m http.server {other} --bind 127.0.0.1"
        foreign = {}

        def take_the_port():
            time.sleep(1.5)
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler)
            srv.RequestHandlerClass.log_message = lambda *a: None
            foreign["srv"] = srv
            srv.serve_forever()

        threading.Thread(target=take_the_port, daemon=True).start()
        try:
            with self.assertRaises(U.UIUnavailable) as cm:
                with U.dev_server(cmd, url, self.tmp, self.log, 20, lock_path=self.lock):
                    self.fail("the capture ran against another server")
            self.assertIn("something other than the dev command answers", str(cm.exception))
            self.assertIn(f"pid {os.getpid()}", str(cm.exception))
            self.assertTrue(U.url_answers(url), "the other server is left alone")
            self.assertFalse(pid_alive(int((self.tmp / "dev.pid").read_text())), "the dev command was stopped")
            self.assertFalse(U.url_answers(f"http://127.0.0.1:{other}"))
        finally:
            if "srv" in foreign:
                foreign["srv"].shutdown()
                foreign["srv"].server_close()
        # the dev command's own listener passes
        with U.dev_server(f"{sys.executable} -m http.server {port} --bind 127.0.0.1", url, self.tmp, self.log, 20,
                          lock_path=self.lock):
            self.assertTrue(U.url_answers(url))
        self.assertFalse(U.url_answers(url))

    def test_where_listeners_cant_be_seen_the_dev_log_decides(self):
        import subprocess
        p = subprocess.Popen(["true"])
        p.wait()
        old = U.listener_pids
        U.listener_pids = lambda port: None
        try:
            self.log.parent.mkdir(parents=True)
            self.log.write_text("old run: Port 5173 is in use, trying another one...\n")
            start = self.log.stat().st_size
            with open(self.log, "a") as fh:
                fh.write("  VITE ready\n  Local: http://localhost:5173/\n")
            self.assertIsNone(U.foreign_listener("http://127.0.0.1:5173", p, self.log, start),
                              "an earlier run's log lines don't count")
            with open(self.log, "a") as fh:
                fh.write("Port 5173 is in use, trying another one...\n  Local: http://localhost:5174/\n")
            why = U.foreign_listener("http://127.0.0.1:5173", p, self.log, start)
            self.assertIn("the dev log says", why or "")
        finally:
            U.listener_pids = old


STATUS_SERVER = textwrap.dedent("""\
    import http.server, sys
    PAGES = {"/": (200, "<title>home</title><script>location.href='/boom'</script>"),
             "/boom": (500, "<title>500</title>Internal Server Error"),
             "/flow": (200, "<title>flow</title><a id=go href='/boom'>go</a>"),
             "/gone": (200, "<title>gone</title><form action='/nope'><button id=go>go</button></form>")}
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            code, body = PAGES.get(self.path.split("?")[0], (404, "<title>404</title>Not Found"))
            b = ('<meta name=viewport content="width=device-width,initial-scale=1">' + body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
""")

NAV_SERVER = textwrap.dedent("""\
    import http.server, sys
    DEAD = sys.argv[2]  # a port nothing listens on
    PAGES = {"/dead": "<title>dead</title><a id=go href='http://127.0.0.1:%s/x'>go</a>" % DEAD,
             "/js-dead": "<title>js</title><button id=go onclick=\\"location.href='http://127.0.0.1:%s/'\\">go</button>" % DEAD,
             "/dl": "<title>dl</title><a id=go href='/file.csv'>download</a>"}
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            if self.path == "/file.csv":
                b = b"a,b\\n1,2\\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=file.csv")
            else:
                b = ('<meta name=viewport content="width=device-width,initial-scale=1">'
                     + PAGES.get(self.path, "<title>404</title>")).encode()
                self.send_response(200 if self.path in PAGES else 404)
                self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
""")

CONTROLS = {
    "covered.html": "<title>covered</title><button id=go onclick=\"document.title='clicked'\">Go</button>"
                    "<div id=overlay class='loading' style='position:fixed;inset:0;background:#fff'>Loading</div>",
    "off.html": "<title>off</title><button id=off style='position:absolute;left:-5000px' "
                "onclick=\"document.title='clicked'\">Off</button>",
    "toast.html": "<title>toast</title><button id=share onclick=\"document.title='shared'\">Share</button>"
                  "<div class=toast style='display:none'>Link copied</div>",
    "faded.html": "<title>faded</title><div style='opacity:0'><p class=toast>Link copied</p></div>",
    "ok.html": "<title>ok</title><button id=share onclick=\"document.querySelector('.toast').style.display='block'\">"
               "Share</button><div class=toast style='display:none'>Link copied</div>"
               "<div id=late style='position:fixed;top:0;left:0'></div>",
}


@unittest.skipUnless(real_chrome(), "no Chrome/Chromium installed")
class RealChromeEvidenceTest(unittest.TestCase):
    """What a shot says must match what is on screen and what a user could do."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ui-evid-"))
        os.environ["ORCHESTRATE_SANDBOX_WRITE"] = str(self.tmp)  # the fake servers' scripts live here
        self.addCleanup(os.environ.pop, "ORCHESTRATE_SANDBOX_WRITE", None)
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self.job = SimpleNamespace(dir=self.tmp / "job", state={"integration_wt": str(self.wt)})
        self.old_wait = U.WAIT_FOR_S
        U.WAIT_FOR_S = 1.5

    def tearDown(self):
        U.WAIT_FOR_S = self.old_wait
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_shot_reports_the_status_of_the_document_on_screen(self):
        server = self.tmp / "server.py"
        server.write_text(STATUS_SERVER)
        port = free_port()
        plan = {"ui": {"dev": f"exec {sys.executable} {server} {port}", "url": f"http://127.0.0.1:{port}",
                       "ready_timeout_s": 30, "viewports": ["desktop"],
                       "pages": [{"path": "/", "name": "redirect"},
                                 {"path": "/flow", "steps": [{"click": "#go"}]},
                                 {"path": "/gone", "steps": [{"click": "#go"}]},
                                 {"path": "/boom"}]}, "tasks": []}
        res = U.capture(plan, self.job, 1)
        self.assertIsNone(res["error"], res["error"])
        shots = {s["name"]: s for s in res["shots"]}
        got = {n: (s["status"], s.get("first_status"), s["title"]) for n, s in shots.items()}
        self.assertEqual(got, {"redirect": (500, 200, "500"), "flow": (500, 200, "500"),
                               "gone": (404, 200, "404"), "boom": (500, None, "500")})
        import orchestrate as O  # the findings no reviewer can wave through now fire for each
        found = O.ui_evidence_findings(res, [])
        for name, sev in (("redirect", "blocker"), ("flow", "blocker"), ("gone", "major"), ("boom", "blocker")):
            self.assertTrue([f for f in found if f["severity"] == sev and f"round-1/{name}--desktop" in f["issue"]],
                            (name, found))

    def test_a_navigation_that_gets_no_response_is_not_the_old_pages_200(self):
        # a click (or a JS redirect) to a dead server leaves Chrome's own error page on screen: that
        # shot has no HTTP response, whatever the first document returned; a download keeps the page
        server = self.tmp / "nav.py"
        server.write_text(NAV_SERVER)
        port, dead = free_port(), free_port()
        plan = {"ui": {"dev": f"exec {sys.executable} {server} {port} {dead}", "url": f"http://127.0.0.1:{port}",
                       "ready_timeout_s": 30, "viewports": ["desktop"],
                       "pages": [{"path": "/dead", "steps": [{"click": "#go"}, {"wait_ms": 800}]},
                                 {"path": "/js-dead", "steps": [{"click": "#go"}, {"wait_ms": 800}]},
                                 {"path": "/dl", "steps": [{"click": "#go"}, {"wait_ms": 800}]}]}, "tasks": []}
        res = U.capture(plan, self.job, 1)
        self.assertIsNone(res["error"], res["error"])
        s = {x["name"]: x for x in res["shots"]}
        for name in ("dead", "js-dead"):
            self.assertIsNone(s[name]["status"], (name, s[name]))
            self.assertEqual(s[name].get("first_status"), 200)
            self.assertTrue(any("failed" in e for e in s[name]["page_errors"]), s[name])
        self.assertEqual(s["dl"]["status"], 200, "a download leaves the page on screen")
        import orchestrate as O
        found = O.ui_evidence_findings(res, [])
        for name in ("dead", "js-dead"):
            self.assertTrue([f for f in found if f["severity"] == "blocker" and f"{name}--desktop" in f["issue"]],
                            (name, found))
        self.assertFalse([f for f in found if "dl--desktop" in f["issue"]], found)

    def test_flow_steps_fail_on_controls_a_user_cannot_use(self):
        for name, body in CONTROLS.items():
            (self.wt / name).write_text('<meta name=viewport content="width=device-width,initial-scale=1">' + body)
        port = free_port()
        plan = {"ui": {"dev": f"exec {sys.executable} -m http.server {port} --bind 127.0.0.1",
                       "url": f"http://127.0.0.1:{port}", "ready_timeout_s": 30, "viewports": ["mobile"],
                       "pages": [{"path": "/covered.html", "steps": [{"click": "#go"}]},
                                 {"path": "/off.html", "steps": [{"click": "#off"}]},
                                 {"path": "/toast.html", "steps": [{"click": "#share"}, {"wait_for": ".toast"}]},
                                 {"path": "/faded.html", "steps": [{"wait_for": ".toast"}]},
                                 {"path": "/ok.html", "name": "bad-selector", "steps": [{"wait_for": "##x"}]},
                                 {"path": "/ok.html", "steps": [{"click": "#share"}, {"wait_for": ".toast"}]}]},
                "tasks": []}
        t0 = time.time()
        res = U.capture(plan, self.job, 1)
        self.assertIsNone(res["error"], res["error"])
        s = {x["name"]: x for x in res["shots"]}
        cov = s["covered-html"]
        self.assertEqual(cov["title"], "covered", "the covered button was not clicked")
        self.assertTrue(any("covered by <div#overlay.loading>" in e and "Loading" in e for e in cov["step_errors"]),
                        cov["step_errors"])
        off = s["off-html"]
        self.assertEqual(off["title"], "off")
        self.assertTrue(any("off-screen" in e for e in off["step_errors"]), off["step_errors"])
        toast = s["toast-html"]
        self.assertEqual(toast["title"], "shared", "the visible button was clicked")
        self.assertTrue(any("wait_for '.toast'" in e for e in toast["step_errors"]), toast["step_errors"])
        self.assertTrue(s["faded-html"]["step_errors"], "an element under an opacity:0 parent is not visible")
        self.assertTrue(any("not a valid CSS selector" in e for e in s["bad-selector"]["step_errors"]),
                        s["bad-selector"]["step_errors"])
        self.assertEqual(s["ok-html"]["step_errors"], [], "a toast shown by the click counts")
        self.assertLess(time.time() - t0, 60, "a bad selector fails at once, not after the wait")

    def test_chrome_opens_no_debugging_port(self):
        if not Path("/proc/net/tcp").exists():
            self.skipTest("needs /proc")
        with U.Chrome() as br:
            self.assertEqual(br.cdp.call("Target.getTargets").get("targetInfos") is not None, True)
            self.assertFalse((Path(br.tmp) / "DevToolsActivePort").exists())
            listening = set()
            for name in ("tcp", "tcp6"):
                for line in Path(f"/proc/net/{name}").read_text().splitlines()[1:]:
                    f = line.split()
                    if f[3] == "0A":
                        listening.add(f"socket:[{f[9]}]")
            held = []
            for d in Path("/proc").iterdir():
                if not d.name.isdigit():
                    continue
                try:
                    if os.getpgid(int(d.name)) != br.proc.pid:
                        continue
                    for fd in (d / "fd").iterdir():
                        if os.readlink(fd) in listening:
                            held.append(d.name)
                except OSError:
                    continue
            self.assertEqual(held, [], "no process of Chrome's listens on a TCP port")

    def test_a_page_that_logs_in_a_loop_keeps_the_queue_small(self):
        with U.Chrome() as br:
            t = U.Tab(br.cdp, "desktop")
            t.navigate("data:text/html,<title>f</title><script>setTimeout(()=>{while(true)console.log('x')},300)"
                       "</script>")
            time.sleep(0.5)
            with self.assertRaises(U.CDPError):
                t.eval("1", timeout=4)  # never answered: the page is busy logging
            self.assertLessEqual(len(br.cdp.events), U.CONSOLE_QUEUE_MAX + 200)
            self.assertGreater(br.cdp.console_dropped.get(t.session, 0), 1000)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(_STATE, ignore_errors=True)
