---
name: inspect-idea-stage
description: "Read-only completion inspector for the W1 idea stage. Checks that /idea-discovery (and its sub-skills /idea-creator, /novelty-check, /research-review, /research-refine, /experiment-plan) left every hand-off artifact complete, gated, and fresh before experiments start; reports a PASS/WARN/FAIL table with file evidence. Does not judge idea quality. Use when user says \"inspect idea stage\", \"检查 idea 阶段\", \"is W1 done\", \"ready for experiments?\", or after /idea-discovery finishes or any idea-stage file is edited."
argument-hint: "[project-dir] [— run: <run_id>, assurance: draft|submission]"
allowed-tools: Bash(*), Read, Grep, Glob, Write
---

# Idea Stage Inspector (W1)

Inspect: $ARGUMENTS

Follow [`shared-references/inspection-protocol.md`](../shared-references/inspection-protocol.md)
for hard rules, verdict states, report files, and staleness. This skill only adds
the W1 checklist.

## Scope

Checks the W1 → W1.5 hand-off: **can `/experiment-bridge` start from these files?**
It does not score ideas, re-check novelty, or call a reviewer. `ABANDON`,
low reviewer scores, or a `REVISE` verdict are reported as facts, not re-judged.

## Constants

- **STAGE = `idea-stage`**
- **ASSURANCE** — from `— assurance:`; default `draft`.
- **RUN_ID** — from `— run:`; else the newest `.aris/runs/*.json` whose phases
  include `idea-creator`. None found → gate row `BLOCKED` (`no_run_record`).

## Step 1: Locate project and previous verdict

```bash
PROJECT_DIR="${PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"  # set from [project-dir] argument
cd "$PROJECT_DIR" || exit 1
ls -la idea-stage/ refine-logs/ .aris/runs/ .aris/inspections/ 2>/dev/null
```

Apply the staleness rule to `.aris/inspections/idea-stage.json` if it exists.
If neither `idea-stage/` nor `refine-logs/` exists → overall `BLOCKED`
(`stage_not_started`), next step `/idea-discovery "<direction>"`; stop.

## Step 2: Run the deterministic evidence gate

Resolve `idea_discovery_gate.py` (Policy E — capture, do not propagate):

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills-codex.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills-codex.txt 2>/dev/null) || true
fi
IDEA_GATE=""
[ -n "${ARIS_REPO:-}" ] && [ -f "$ARIS_REPO/tools/idea_discovery_gate.py" ] && IDEA_GATE="$ARIS_REPO/tools/idea_discovery_gate.py"
[ -z "$IDEA_GATE" ] && [ -f tools/idea_discovery_gate.py ] && IDEA_GATE="tools/idea_discovery_gate.py"
if [ -n "$IDEA_GATE" ] && [ -n "${RUN_ID:-}" ]; then
  python3 - "$IDEA_GATE" "$RUN_ID" <<'PY'; gate_exit=$?
import sys, pathlib
gate_path, run_id = sys.argv[1], sys.argv[2]
sys.path.insert(0, str(pathlib.Path(gate_path).resolve().parent))
import idea_discovery_gate as gate, run_state
try:
    state = run_state.load_run(".", run_id)
except FileNotFoundError:
    print("BLOCKED: run state missing"); sys.exit(1)
result = gate.evaluate(".", state)
print(f"VERDICT: {result.verdict}")
for reason in result.reasons:
    print(f"BLOCKED: {reason}")
sys.exit(0 if result.verdict == "PASS" else 1)
PY
  echo "idea_discovery_gate evaluate exit=$gate_exit"
else
  echo "WARN: idea_discovery_gate.py or RUN_ID not resolved; row I1 = BLOCKED" >&2
fi
```

Call `evaluate()` only. **Never run the gate's CLI** (`python3 "$IDEA_GATE" . <run>`):
its `main()` rewrites the gate block in `IDEA_REPORT.md` and records a result in
`.aris/runs/<run_id>.json`, which would break the read-only rule and make the
inspection itself change the artifact it inspects.

## Step 3: W1 checklist

| # | Requirement | How to check | Fail state |
|---|-------------|--------------|------------|
| I1 | Evidence gate passes | Step 2 exit 0 | non-zero → `FAIL`; unresolved → `BLOCKED` |
| I2 | `idea-stage/IDEA_REPORT.md` exists, non-empty | `Read` | `FAIL` |
| I3 | Report has all H2 sections with content: `## Executive Summary`, `## Literature Landscape`, `## Ranked Ideas`, `## Novelty Verification`, `## External Critical Review`, `## Eliminated Ideas`, `## Refined Proposal`, `## Next Steps` | `Grep -n "^## "`, then confirm each has body text | missing/empty → `FAIL` |
| I4 | Exactly one idea marked `— RECOMMENDED`, with `Pilot:`, `Novelty:`, `Reviewer score:` lines | `Grep "RECOMMENDED"` | none → `FAIL`; >1 → `WARN` |
| I5 | Recommended idea's novelty verdict is `PROCEED` or `PROCEED WITH CAUTION`; prior work not tagged `[UNVERIFIED]` only | Novelty section | `ABANDON` → `FAIL`; only `[UNVERIFIED]` evidence → `WARN` |
| I6 | Pilot signal recorded (`POSITIVE` / `WEAK POSITIVE` / `NEGATIVE` / `SKIPPED`) | `## Pilot Experiment Results` or per-idea `Pilot` line | `SKIPPED` / "needs manual pilot" → `WARN` |
| I7 | `idea-stage/docs/research_contract.md` exists and names the selected idea, core claims, minimum convincing evidence | `Read` | `FAIL` (this is the W1→W1.5 contract) |
| I8 | `refine-logs/FINAL_PROPOSAL.md` exists, starts `# Research Proposal:` | `Read` first lines | `FAIL` |
| I9 | `refine-logs/REFINE_STATE.json` `status == "completed"`; record `last_verdict` and `last_score` | parse JSON | `in_progress` → `FAIL`; `last_verdict` `REVISE` with weaknesses documented in `REVIEW_SUMMARY.md` → `WARN`; `RETHINK` → `FAIL` |
| I10 | `refine-logs/EXPERIMENT_PLAN.md` has `## Claim Map`, `## Experiment Blocks`, `## Run Order and Milestones`, `## Compute and Data Budget` | `Grep "^## "` | `FAIL` |
| I11 | Every claim in the Claim Map links to ≥1 experiment block, and ≥1 block is `MUST-RUN` | read Claim Map + blocks | `FAIL` |
| I12 | `refine-logs/EXPERIMENT_TRACKER.md` table header `\| Run ID \| Milestone \| … \| Status \| Notes \|` present with ≥1 row | `Read` | `FAIL` |
| I13 | `idea-stage/IDEA_CANDIDATES.md` has `## Active Idea` and a status column | only if the run used `COMPACT=true` | absent → `NOT_APPLICABLE` |
| I14 | `REF_PAPER_SUMMARY.md` exists | only if a reference paper was given | `NOT_APPLICABLE` otherwise |
| I15 | Research wiki recorded ideas | only if `research-wiki/` exists: `research-wiki/ideas/` non-empty and `log.md` has an `idea-creator wrote` line | missing → `WARN` |
| I16 | State files are fresh | `REFINE_STATE.json` mtime < 24 h before the newest W1 artifact, per [`output-versioning.md`](../shared-references/output-versioning.md) | older → `WARN` (may be from a previous direction) |
| I17 | Fixed-name files match their newest timestamped copy (`IDEA_REPORT_<ts>.md` vs `IDEA_REPORT.md`) when timestamped copies exist | compare hashes | mismatch → `WARN` |

`WARN` rows never block the hand-off; list them so the user decides.

## Step 4: Report

Write `.aris/inspections/idea-stage_<ts>.{md,json}` and the fixed-name copies,
with `inspected_files` = every file read in Step 3. Then show the user-facing table
from the protocol.

**Next step** by verdict:
- `PASS` / `WARN` → `/experiment-bridge` (reads `refine-logs/EXPERIMENT_PLAN.md`).
- `FAIL` on I1–I6 → re-run `/idea-discovery` (or the named sub-skill).
- `FAIL` on I8–I12 → `/research-refine-pipeline` or `/experiment-plan`.

## Key Rules

- Never edit an idea-stage or refine-logs file; never call `/novelty-check` or a reviewer.
- Never mark `I1` `PASS` without the gate's exit code in the evidence.
- A `REVISE` refine verdict is acceptable only with its weaknesses written down (I9).
