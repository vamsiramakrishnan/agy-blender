# Skill trigger set

Does an agent pick the right one of the plugin's six skills from its name and description alone?
`triggers.json` holds 32 requests (18 should, 6 near-miss, 4 combo, 4 none) in the format of
awesome-gamedev-agent-skills' `evals/README.md`. `run_trigger_evals.py` is adapted from that repository's
`scripts/run-trigger-evals.py` (commit c6ca6c4, Apache-2.0, licence in `UPSTREAM-LICENSE.txt`; what changed is
listed at the top of the script). The suite runs `--check` (offline) through `tests/test_skills.py`.

Score the descriptions of a checkout with a Gemini judge (needs `GEMINI_API_KEY` in the environment; the judge
sends only the selector prompt, and uses `gemini-4-hydrogen` unless `--model` or `AGY3D_SKILL_JUDGE_MODEL` says
otherwise). Run from an empty folder so the judge does not load anything of this repository:

```bash
REPO=/path/to/checkout
cd "$(mktemp -d)"
# after: the descriptions in the working tree
python $REPO/benchmarks/skill_triggers/run_trigger_evals.py --root $REPO --jobs 4 --json \
  --judge-cmd "python $REPO/benchmarks/skill_triggers/gemini_judge.py" > after.json
# before: the descriptions at commit 29660b8 against the same file
mkdir before && git -C $REPO archive 29660b8 plugin/antigravity-3d/skills | tar -x -C before
python $REPO/benchmarks/skill_triggers/run_trigger_evals.py --root "$PWD/before" --jobs 4 --json \
  --judge-cmd "python $REPO/benchmarks/skill_triggers/gemini_judge.py" > before.json
```

Compare the `summary` blocks (`kinds`, `precision`, `recall`, `worst_skills`). The same model must judge both
sides, and differences of a few points are noise. 32 judge calls per side, each about 1,500 input tokens.
