---
name: inspect-experiment-stage
description: "Read-only completion inspector for the W1.5 experiment stage. Checks that /experiment-bridge, /run-experiment, /experiment-audit and /result-to-claim left the tracker, log, results, audit verdict and claim records complete, consistent and fresh before review or paper writing; re-verifies cited numbers against raw files with the deterministic evidence_check.py. Does not judge whether results are good. Use when user says \"inspect experiment stage\", \"检查实验阶段\", \"are experiments done\", \"ready for review?\", or after experiments finish or any refine-logs result file is edited."
argument-hint: "[project-dir] [— assurance: draft|submission]"
allowed-tools: Bash(*), Read, Grep, Glob, Write
---

# Experiment Stage Inspector (W1.5)

Inspect: $ARGUMENTS

Follow [`shared-references/inspection-protocol.md`](../shared-references/inspection-protocol.md)
for hard rules, verdict states, report files, and staleness. This skill only adds
the W1.5 checklist.

## Scope

Checks the W1.5 → W2/W3 hand-off: **is every planned run accounted for, recorded,
audited, and mapped to a claim verdict?** It does not re-audit eval code (that is
`/experiment-audit`, per [`experiment-integrity.md`](../shared-references/experiment-integrity.md))
and does not decide whether a claim is supported (that is `/result-to-claim`).

## Constants

- **STAGE = `experiment-stage`**
- **ASSURANCE** — from `— assurance:`; default `draft`. At `submission`, a missing
  `EXPERIMENT_AUDIT.json` is `FAIL` instead of `WARN`.

## Step 1: Locate project and previous verdict

```bash
PROJECT_DIR="${PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"  # set from [project-dir] argument
cd "$PROJECT_DIR" || exit 1
ls -la refine-logs/ .aris/ .aris/inspections/ 2>/dev/null
ls EXPERIMENT_AUDIT.* refine-logs/EXPERIMENT_AUDIT.* 2>/dev/null
```

Apply the staleness rule to `.aris/inspections/experiment-stage.json`.
If `refine-logs/EXPERIMENT_PLAN.md` is missing → overall `BLOCKED`
(`prerequisite_stage_missing`), next step `/inspect-idea-stage`; stop.

## Step 2: Tracker ↔ log reconciliation

Parse the `refine-logs/EXPERIMENT_TRACKER.md` table (`| Run ID | Milestone | … | Priority | Status | Notes |`)
into rows. For every run, look for its entry in `refine-logs/EXPERIMENT_LOG.md`
(`## Experiment: <name>` blocks; match by Run ID or name in the block).

Build this table for the report:

| Run ID | Priority | Tracker Status | Log entry | Verdict line | Reproduction cmd |
|--------|----------|----------------|-----------|--------------|------------------|

## Step 3: Deterministic evidence re-check

If `.aris/claims.json` exists (written by `/result-to-claim`), resolve
`evidence_check.py` (Policy E) and re-run it — it only reads:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
fi
if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
    ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
fi
EVIDENCE_CHECK=".aris/tools/evidence_check.py"
[ -f "$EVIDENCE_CHECK" ] || EVIDENCE_CHECK="tools/evidence_check.py"
[ -f "$EVIDENCE_CHECK" ] || { [ -n "${ARIS_REPO:-}" ] && EVIDENCE_CHECK="$ARIS_REPO/tools/evidence_check.py"; }
[ -f "$EVIDENCE_CHECK" ] || EVIDENCE_CHECK=""
if [ -n "$EVIDENCE_CHECK" ] && [ -f .aris/claims.json ]; then
  python3 "$EVIDENCE_CHECK" . --batch .aris/claims.json; ev_exit=$?
  echo "evidence_check exit=$ev_exit"
else
  echo "WARN: evidence_check.py or .aris/claims.json not available; row E9 = BLOCKED/NOT_APPLICABLE" >&2
fi
```

`verified` means only that the value exists in its cited source — not that the
claim is right. Report `path_missing` / `value_not_found` items verbatim.

## Step 4: W1.5 checklist

| # | Requirement | How to check | Fail state |
|---|-------------|--------------|------------|
| E1 | `refine-logs/EXPERIMENT_TRACKER.md` exists with ≥1 run row | Step 2 | `FAIL` |
| E2 | Every `MUST` / `MUST-RUN` run is `DONE` | Step 2 | `TODO` / `RUNNING` → `FAIL`; failed run with reason in Notes → `WARN` |
| E3 | Every `DONE` run has an `EXPERIMENT_LOG.md` entry with `### Setup`, `### Results`, `### Verdict` (`**Supports claim?** Yes / Partially / No`), `### Reproduction` | Step 2 | missing entry → `FAIL`; missing Reproduction → `WARN` |
| E4 | No orphan log entries (logged but absent from tracker) | Step 2 | `WARN` |
| E5 | `refine-logs/EXPERIMENT_RESULTS.md` has `## Results by Milestone` and an `M0` sanity line marked `PASSED` | `Read` | missing → `FAIL`; M0 not passed → `FAIL` |
| E6 | Numbers in `EXPERIMENT_RESULTS.md` tables trace to a raw file (`results/`, `outputs/`, `experiments/`, `.json/.jsonl/.csv/.tsv`) | spot-check ≤5 values with `Grep` in raw files | value not found → `FAIL` |
| E7 | `EXPERIMENT_AUDIT.json` exists with `overall_verdict` and `checks.{gt_provenance, score_normalization, result_existence, dead_code, scope}` and `eval_type` | parse JSON (it uses the legacy shape, lowercase `integrity_status`) | missing → `WARN` (draft) / `FAIL` (submission); `overall_verdict` FAIL → `FAIL`; WARN → `WARN` |
| E8 | Audit is newer than the newest result file it covers | compare mtimes of `EXPERIMENT_AUDIT.json` vs `EXPERIMENT_RESULTS.md` / raw results | older → `WARN` (`audit_stale`) |
| E9 | Deterministic evidence check passes | Step 3 exit 0 | non-zero → `FAIL`; no `claims.json` → `NOT_APPLICABLE` |
| E10 | Every claim in the Claim Map of `EXPERIMENT_PLAN.md` has a `/result-to-claim` verdict (`claim_supported: yes / partial / no`, with confidence) | `Grep claim_supported` across outputs, or wiki `research-wiki/claims/` / `experiments/` nodes | none → `FAIL`; some claims missing → `FAIL` |
| E11 | Claims with `[INTEGRITY CONCERN]` or `confidence: low` are listed, not silently promoted | `Grep "INTEGRITY"` | present → `WARN` with list |
| E12 | `findings.md` has a postmortem line for every `claim_supported: no` | `Grep` | missing → `WARN` |
| E13 | Research wiki has `exp:` nodes and `supports` / `invalidates` edges for each verdict | only if `research-wiki/` exists; `add_edge` does not verify nodes, so check both ends exist | dangling edge → `WARN` |
| E14 | `idea-stage/docs/research_contract.md` updated after main results ("major results obtained" milestone) | mtime newer than newest `DONE` run log entry | older → `WARN` |

## Step 5: Report

Write `.aris/inspections/experiment-stage_<ts>.{md,json}` and fixed-name copies;
`inspected_files` = tracker, log, results, audit, claims, contract, and every raw
file touched in E6. Include the Step 2 reconciliation table in the `.md` report.

**Next step** by verdict:
- `PASS` / `WARN` → `/auto-review-loop "<scope>"` (W2), or `/paper-writing` if review is done.
- `FAIL` on E2/E3/E5 → `/experiment-bridge` (resume) or `/run-experiment` for the named runs.
- `FAIL` on E7/E8 → `/experiment-audit`.
- `FAIL` on E9/E10 → `/result-to-claim`.

## Key Rules

- Never edit the tracker, log, results, audit, or claims files — even to tick a box.
- Never decide a claim verdict yourself; only check that one exists and is traceable.
- Report raw-file paths for every number you spot-checked.
