---
name: inspect-paper-stage
description: "Read-only completion inspector for the W2 review loop and W3 paper writing. Checks that /auto-review-loop terminated correctly, that PAPER_PLAN, LaTeX sources, compiled PDF and round snapshots exist, that the compile is clean, and that the mandatory audits (proof, claim, citation, kill-argument) are present, schema-valid and not stale — by running the external verify_paper_audits.sh. Does not judge paper quality. Use when user says \"inspect paper stage\", \"检查论文阶段\", \"is the paper ready\", \"pre-submission check\", or after /paper-writing finishes or any paper/ file is edited."
argument-hint: "[project-dir] [— paper: paper/, assurance: draft|submission]"
allowed-tools: Bash(*), Read, Grep, Glob, Write
---

# Paper Stage Inspector (W2 + W3)

Inspect: $ARGUMENTS

Follow [`shared-references/inspection-protocol.md`](../shared-references/inspection-protocol.md)
for hard rules, verdict states, report files, and staleness. This skill only adds
the W2 + W3 checklist.

## Scope

Checks the hand-off to submission / W4–W6: **did the review loop end by its own
rules, does the paper build cleanly, and does every mandatory audit have a valid,
current verdict?** It does not review the paper, re-score it, or run any audit
skill. Audit content belongs to `/paper-claim-audit`, `/citation-audit`,
`/kill-argument`, `/proof-checker`.

## Constants

- **STAGE = `paper-stage`**
- **PAPER_DIR** — from `— paper:`; default `paper/`.
- **ASSURANCE** — from `— assurance:`; else `<PAPER_DIR>/.aris/assurance.txt`; else `draft`.
  Only `draft` and `submission` are valid (per [`assurance-contract.md`](../shared-references/assurance-contract.md)); any other value → row P0 `ERROR`.
- **MAX_ROUNDS = 4**, early stop requires **score ≥ 6 AND verdict ∈ {ready, almost}** (from `/auto-review-loop`).

## Step 1: Locate project and previous verdict

```bash
PROJECT_DIR="${PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"  # set from [project-dir] argument
cd "$PROJECT_DIR" || exit 1
ls -la review-stage/ "${PAPER_DIR:-paper}/" "${PAPER_DIR:-paper}/.aris/" .aris/inspections/ 2>/dev/null
ls AUTO_REVIEW.md REVIEW_STATE.json NARRATIVE_REPORT.md PAPER_PLAN.md PAPER_ACCEPTANCE_CONTRACT.md 2>/dev/null
```

Legacy layout: `AUTO_REVIEW.md` / `REVIEW_STATE.json` may sit in the project root
instead of `review-stage/` — accept either, note which.
Apply the staleness rule to `.aris/inspections/paper-stage.json`.

## Step 2: Run the external audit verifier

Resolve `verify_paper_audits.sh` (Policy E here — the inspector reports, it does not
gate). Write its JSON **into `.aris/inspections/`**, never into `<PAPER_DIR>/.aris/`,
so the inspection does not overwrite the report `/paper-writing` Phase 6 relies on:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 1
if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills-codex.txt ]; then
    ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills-codex.txt 2>/dev/null) || true
fi
AUDIT_VERIFIER=""
[ -n "${ARIS_REPO:-}" ] && [ -f "$ARIS_REPO/tools/verify_paper_audits.sh" ] && AUDIT_VERIFIER="$ARIS_REPO/tools/verify_paper_audits.sh"
[ -z "$AUDIT_VERIFIER" ] && [ -f tools/verify_paper_audits.sh ] && AUDIT_VERIFIER="tools/verify_paper_audits.sh"
mkdir -p .aris/inspections
if [ -n "$AUDIT_VERIFIER" ]; then
  bash "$AUDIT_VERIFIER" "${PAPER_DIR:-paper}" --assurance "${ASSURANCE:-draft}" \
       --json-out .aris/inspections/paper-stage_verifier.json; verifier_exit=$?
  echo "verify_paper_audits exit=$verifier_exit"
else
  echo "WARN: verify_paper_audits.sh not resolved; rows P14-P15 = BLOCKED" >&2
fi
```

Exit `0` = green, `1` = blocking issue, `2` = bad arguments (→ `ERROR`). At `draft`,
exit 0 means only "audits, where present, are well-formed" — still read the JSON rows
(`MISSING`, `STALE`, `BLOCKING_VERDICT`, `SCHEMA_INVALID`).

## Step 3: W2 + W3 checklist

| # | Requirement | How to check | Fail state |
|---|-------------|--------------|------------|
| P0 | Assurance level valid | Constants | `ERROR` |
| **W2 — review loop** | | | |
| P1 | `AUTO_REVIEW.md` has one entry per round (score, verdict, actions) and a final summary with `## Method Description` | `Grep -n "^## "` | `FAIL` |
| P2 | `REVIEW_STATE.json` `status == "completed"` | parse JSON | `in_progress` → `FAIL` |
| P3 | Termination is legal: `last_score ≥ 6` AND `last_verdict ∈ {ready, almost}`, **or** `round == 4` with remaining blockers + effort estimates + continue/pivot advice in `AUTO_REVIEW.md` | JSON + log | neither → `FAIL`; max-rounds exit with blockers listed → `WARN` |
| P4 | Reviewer independence recorded: `reviewer_family`, `family_relation`, `independence_verified` | JSON keys | same-family / unverified → `WARN` (`acceptance_status: provisional`) |
| P5 | `pending_experiments` empty | JSON | non-empty → `FAIL` |
| P6 | `CLAIMS_FROM_RESULTS.md`, if present, does not start with `verdict: REVIEW_UNAVAILABLE` | `Read` first lines | present with that line → `WARN` (claims unadjudicated) |
| **W3 — paper** | | | |
| P7 | `NARRATIVE_REPORT.md` has problem/claim, method, key results with evidence, figure/table inventory, limitations | `Read` | missing → `FAIL`; section missing → `WARN` |
| P8 | `PAPER_PLAN.md` exists (project root) with section and figure plan | `Read` | `FAIL` |
| P9 | `<PAPER_DIR>/main.tex` exists and `\input`s every file in `<PAPER_DIR>/sections/` | `Grep "\\input"` vs `Glob sections/*.tex` | file not input → `FAIL` |
| P10 | No unresolved markers in sources: `[VERIFY]`, `TODO`, `<!-- DATA_NEEDED` | `Grep -rn` in `<PAPER_DIR>` | any → `FAIL` at submission, `WARN` at draft |
| P11 | `<PAPER_DIR>/main.pdf` exists, > 100 KB, newer than every `.tex` / `.bib` | `ls -la`, mtimes | missing → `FAIL`; older than sources → `FAIL` (`pdf_stale`) |
| P12 | Compile clean: `compile.log` (or `main.log`) has 0 `LaTeX Warning.*undefined` and 0 `Citation.*undefined` | `grep -c` | > 0 → `FAIL` |
| P13 | Round snapshots preserved: `main_round0_original.pdf` plus `main_round<N>.pdf` for each round in `PAPER_IMPROVEMENT_STATE.json` (`current_round`), and that state is `completed` | `ls`, JSON | missing snapshot → `WARN`; state `in_progress` → `FAIL` |
| **Audits** | | | |
| P14 | Verifier exit code | Step 2 | `1` → `FAIL`; `2` → `ERROR`; unresolved → `BLOCKED` |
| P15 | Per-audit rows for `PROOF_AUDIT.json`, `PAPER_CLAIM_AUDIT.json`, `CITATION_AUDIT.json`, `KILL_ARGUMENT.json`: verdict + reason_code + verifier row state | read `.aris/inspections/paper-stage_verifier.json` | `MISSING` / `STALE` / `SCHEMA_INVALID` / `BLOCKING_VERDICT` → `FAIL` at submission, `WARN` at draft; `NOT_APPLICABLE` must carry its reason code |
| P16 | `PAPER_ACCEPTANCE_CONTRACT.md` has no `violated` undisputed assertion | only if file exists; the verifier does not read it | violated → `FAIL` |
| P17 | Integrity forensics gate not `BLOCK` | `<PAPER_DIR>/.aris/forensics/` latest gate; `opt_out.txt` → `NOT_APPLICABLE` with note; the verifier does not read it | `BLOCK` → `FAIL` at submission |
| P18 | `paper-writing` final verifier report exists and its `overall_assurance` is `accepted` (submission only) | `<PAPER_DIR>/.aris/audit-verifier-report.json` | `provisional` → `WARN`; `blocked` → `FAIL`; absent at submission → `FAIL` |

## Step 4: Report

Write `.aris/inspections/paper-stage_<ts>.{md,json}` and fixed-name copies;
`inspected_files` = review log/state, narrative, plan, `main.tex`, every
`sections/*.tex`, `.bib`, `main.pdf`, and each audit JSON. Keep
`paper-stage_verifier.json` beside them as raw evidence.

**Next step** by verdict:
- `PASS` → submit, or `/paper-talk` / `/rebuttal` later; `/inspect-post-paper` after those.
- `FAIL` on P1–P6 → `/auto-review-loop` (resume from `REVIEW_STATE.json`).
- `FAIL` on P9–P13 → `/paper-compile` or `/auto-paper-improvement-loop`.
- `FAIL` on P14–P15 → run the named audit (`/paper-claim-audit`, `/citation-audit`, `/kill-argument`, `/proof-checker`), then re-inspect.

## Key Rules

- Never pass `--json-out` pointing inside `<PAPER_DIR>`; never edit `.tex`, `.bib`, PDFs, or audit JSON.
- Never run an audit skill or a reviewer from this skill.
- A `PASS` here is a completeness verdict, not a quality or acceptance prediction — say so in the summary.
