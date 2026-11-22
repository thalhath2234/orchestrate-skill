.PHONY: test eval-quick eval-parent eval eval-sweep-manager eval-sweep-classifier eval-sweep-reviewer eval-sweep-advisor eval-sweep-worker validate-plugin release-check live-smoke

# Harness tests: fake `claude`, no tokens spent. CI (.github/workflows/test.yml) runs this target on
# Linux, Python 3.9 and 3.13. Evals and live-smoke never run in CI: they cost tokens.
# The browser tests skip without Chrome, so CI checks for one first (`ui_review.py chrome`).
test:
	python3 tests/test_routing.py
	python3 tests/test_orchestrate.py
	python3 tests/test_evals.py
	python3 tests/test_ui_review.py
	python3 tests/test_dashboard.py
	python3 tests/test_packaging.py

# Prompt evals: real `claude`, real cost. Run the core set on every SKILL.md / prompt change.
# The core set includes 2 parent cases (Opus sessions of the real skill) and 2 UI reviewer cases.
eval-quick:
	env -u ORCHESTRATE_DIR python3 evals/run.py --core --min-pass 0.9 \
	  --json evals/runs/quick-$$(date +%Y%m%d-%H%M%S).json

# The parent skill and the UI reviewer alone, every case, 1 trial.
eval-parent:
	env -u ORCHESTRATE_DIR python3 evals/run.py --role parent --role ui_reviewer --trials 1 \
	  --json evals/runs/parent-$$(date +%Y%m%d-%H%M%S).json

# Every case, 3 trials (consistency). Nightly / before a release (with validate-plugin, release-check
# and live-smoke; bump .claude-plugin/plugin.json in any PR that changes skills/, hooks/ or .claude-plugin).
eval:
	env -u ORCHESTRATE_DIR python3 evals/run.py --trials 3 --json evals/last-run.json

# Pick the manager's model by measurement (P3.4): keep the cheapest within 2 points of the best.
eval-sweep-manager:
	env -u ORCHESTRATE_DIR python3 evals/run.py --role manager --trials 2 \
	  --config opus/medium --config opus/high --config sonnet/high \
	  --json evals/runs/sweep-manager-$$(date +%Y%m%d-%H%M%S).json

eval-sweep-classifier:
	env -u ORCHESTRATE_DIR python3 evals/run.py --role classifier --trials 3 \
	  --config haiku/low --config sonnet/low \
	  --json evals/runs/sweep-classifier-$$(date +%Y%m%d-%H%M%S).json

# Advisor: Fable 5.2 at medium (default; re-asked at high when nothing blocks) against high only.
eval-sweep-advisor:
	env -u ORCHESTRATE_DIR python3 evals/run.py --role advisor --trials 2 \
	  --config fable/medium --config fable/high \
	  --json evals/runs/sweep-advisor-$$(date +%Y%m%d-%H%M%S).json

# Worker prompt: Opus 5.5 at medium (default) against low.
eval-sweep-worker:
	env -u ORCHESTRATE_DIR python3 evals/run.py --role worker --trials 2 \
	  --config opus/medium --config opus/low \
	  --json evals/runs/sweep-worker-$$(date +%Y%m%d-%H%M%S).json

# Final reviewer: Opus 5.5 at medium (default) against stronger settings.
eval-sweep-reviewer:
	env -u ORCHESTRATE_DIR python3 evals/run.py --role reviewer --trials 2 \
	  --config opus/medium --config opus/high --config fable/high \
	  --json evals/runs/sweep-reviewer-$$(date +%Y%m%d-%H%M%S).json

# Before tagging a release vX.Y.Z: run validate-plugin and release-check.
validate-plugin:
	claude plugin validate .

# Fails when skills/, hooks/ or .claude-plugin changed since the last vX.Y.Z tag and the version in
# .claude-plugin/plugin.json didn't: plugin installs only update when that version changes.
release-check:
	python3 tests/check_release.py

# One Haiku call (about a cent): the plugin's approval hook really stops an `--approve` under Claude Code.
# Run before each release and after a Claude Code update that changes hooks or -p.
live-smoke:
	ORCHESTRATE_LIVE=1 python3 tests/live_plugin.py -v
