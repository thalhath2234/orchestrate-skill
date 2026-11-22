# Result: confounded and incomplete

Consistent with keeping opus/medium (the rule in RULE.md is not met). Not evidence that high and
medium detect equally well: read these limits before quoting any number.

- `advisorModel: opus` was in the user settings (saved 14:53, before the sweep started at 15:02).
  The eval's hermetic mode doesn't clear it. All 21 completed reviews consulted a second Opus,
  in both arms, so neither arm measured the reviewer alone.
- Stopped for cost at about $17.8 recorded, partway through block 3 of 4. Block 3 has text rows
  only (3 of 9 cases), block 4 never ran.
- The advisor calls were about $7 of that.

As recorded (confounded): medium 6/6 defects and 3/3 controls; high 7/7 defects and 3/5 controls.
In both high control failures the reviewer's own verdict was `pass`. `enforce_edge_cases` added
majors with no `confidence` for cases the reviewer had doubts about, and those block. Replayed
without those added findings, every completed trial in both arms passes its grader. The same
happens in `quick-20261122-144116` (medium, no advisor) for `clean-diff-passes`.
