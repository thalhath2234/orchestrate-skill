.PHONY: test eval-quick eval eval-sweep-manager eval-sweep-classifier validate-plugin

# Harness tests: fake `claude`, no tokens spent.
test:
	python3 tests/test_routing.py
	python3 tests/test_orchestrate.py
	python3 tests/test_evals.py

# Prompt evals: real `claude`, real cost. Run the core set on every SKILL.md / prompt change.
eval-quick:
	env -u ORCHESTRATE_DIR python3 evals/run.py --core --min-pass 0.9

# Every case, 3 trials (consistency). Nightly / before a release.
eval:
	env -u ORCHESTRATE_DIR python3 evals/run.py --trials 3 --json evals/last-run.json

# Pick the manager's model by measurement (P3.4): keep the cheapest within 2 points of the best.
eval-sweep-manager:
	env -u ORCHESTRATE_DIR python3 evals/run.py --role manager --trials 2 \
	  --config opus/high --config opus/medium --config sonnet/high

eval-sweep-classifier:
	env -u ORCHESTRATE_DIR python3 evals/run.py --role classifier --trials 3 \
	  --config haiku/low --config sonnet/low

validate-plugin:
	claude plugin validate .
