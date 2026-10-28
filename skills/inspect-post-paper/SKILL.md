---
name: inspect-post-paper
description: "Read-only completion inspector for post-paper workflows: W4 /rebuttal, W5 /resubmit-pipeline, W6 /paper-talk (+ /slides-polish). Checks required deliverables, hard limits (rebuttal character budget, page limit, 16:9 export parity), the provenance / commitment / coverage gates, and that audit verdicts are present and current. Does not judge persuasiveness or slide design. Use when user says \"inspect rebuttal\", \"inspect resubmit\", \"inspect talk\", \"检查 rebuttal\", \"检查重投\", \"检查 talk\", or before pasting a rebuttal, uploading a resubmission, or presenting."
argument-hint: "[rebuttal|resubmit|talk] [dir] [— limit: <chars>, assurance: draft|submission]"
allowed-tools: Bash(*), Read, Grep, Glob, Write
---

# Post-Paper Inspector (W4 / W5 / W6)

Inspect: $ARGUMENTS

Follow [`shared-references/inspection-protocol.md`](../shared-references/inspection-protocol.md)
for hard rules, verdict states, report files, and staleness. This skill only adds
the W4 / W5 / W6 checklists.

## Scope

Checks that a post-paper deliverable is **complete and inside its hard limits**
before it leaves the machine. It does not rewrite the rebuttal, re-run the
resubmit loop, or polish slides.

## Constants

- **STAGE = `post-paper`** (report files use `post-paper_<target>_<ts>`).
- **TARGET** — first argument: `rebuttal` | `resubmit` | `talk`. If omitted, detect:
  `rebuttal/` exists → rebuttal; a `RESUBMIT_REPORT.json` exists → resubmit;
  `slides/` or `.aris/paper-talk/` exists → talk. Several → inspect each. None →
  `BLOCKED` (`nothing_to_inspect`).
- **LIMIT** — from `— limit:` (rebuttal characters). Else read the venue limit
  recorded in `rebuttal/REBUTTAL_STATE.md`. Unknown → row R6 `BLOCKED`.

## Step 1: Previous verdict

Apply the staleness rule to `.aris/inspections/post-paper_<target>.json`.

## Step 2A: W4 rebuttal (`rebuttal/`)

Determine the mode from `REBUTTAL_STATE.md`: `single_document`, `per_reviewer_thread`, or QUICK_MODE.

| # | Requirement | How to check | Fail state |
|---|-------------|--------------|------------|
| R1 | Always-present files: `REVIEWS_RAW.md`, `REBUTTAL_STATE.md`, `ISSUE_BOARD.md`, `STRATEGY_PLAN.md` | `ls` | missing → `FAIL` |
| R2 | Full mode also has `REVISION_PLAN.md` and ≥1 `MCP_STRESS_TEST_round<N>.md` (N ≤ 5) | `ls` | `FAIL`; QUICK_MODE → `NOT_APPLICABLE` |
| R3 | `single_document`: `REBUTTAL_DRAFT_v1.md`, `REBUTTAL_DRAFT_rich.md`, `PASTE_READY.txt` exist | `ls` | `FAIL` |
| R4 | `per_reviewer_thread`: one `Reviewer_<ID>_response.md` per reviewer ID found in `REVIEWS_RAW.md`; no `PASTE_READY.txt` expected | compare IDs | missing reviewer → `FAIL` |
| R5 | Coverage gate: every concern in `ISSUE_BOARD.md` is `answered`, `deferred_intentionally`, or `needs_user_input` | `Grep` status column | any other / blank → `FAIL`; `needs_user_input` remaining → `WARN` |
| R6 | `PASTE_READY.txt` character count ≤ LIMIT | `python3 -c "print(len(open('rebuttal/PASTE_READY.txt',encoding='utf-8').read()))"` | over → `FAIL` |
| R7 | Provenance gate: every factual statement tagged `paper` / `review` / `user_confirmed_result` / `user_confirmed_derivation` / `future_work` in the draft or issue board | `Grep` tags; spot-check 5 numbers against `paper/` | untagged numbers → `FAIL` |
| R8 | Commitment gate: every promise is `already_done` / `approved_for_rebuttal` / `future_work_only`, and every promised paper edit is a checklist item in `REVISION_PLAN.md` | cross-check | unmatched promise → `FAIL` |
| R9 | Final stress test (highest N) has no unresolved critical item | `Read` last `MCP_STRESS_TEST_round<N>.md` | open critical → `WARN` |
| R10 | `PASTE_READY.txt` is plain text (no Markdown headings, no `[OPTIONAL — cut if over limit]` markers left) | `Grep` | present → `FAIL` |

## Step 2B: W5 resubmit (`<NEW_VENUE_DIR>/`)

Locate `RESUBMIT_REPORT.json` (it names the source dir and target venue).

| # | Requirement | How to check | Fail state |
|---|-------------|--------------|------------|
| S1 | `RESUBMIT_REPORT.{md,json}` exist; `audit_skill == "resubmit-pipeline"` | parse | `FAIL` |
| S2 | `verdict` / `reason_code` pair is one of the documented ledger states (`PASS clean_resubmit`, `WARN partially_addressed_concerns`, `FAIL kill_argument_unresolved_critical`, `NOT_APPLICABLE not_a_resubmit`, `USER_DECISION awaiting_phase_<N>_checkpoint`, `BLOCKED <reason>`, `ERROR audit_failure \| loop_failure`) | parse | unknown pair → `ERROR`; `FAIL` / `BLOCKED` / `ERROR` → `FAIL`; `USER_DECISION` → `BLOCKED` (awaiting user) |
| S3 | Source paper dir unchanged: SHA256 of inputs listed in the report still match | re-hash listed inputs | mismatch → `FAIL` (`base_dir_modified`) |
| S4 | Required files in the new dir: `main.tex`, `main.pdf`, `compile.log`, `BASELINE.md`, `PAPER_CLAIM_AUDIT.json`, `CITATION_AUDIT.json`, `KILL_ARGUMENT.json`, `PROOF_AUDIT.json`, `COMPILE_REPORT.json`, `PAPER_IMPROVEMENT_LOG.md`, `.aris/DIFF_REPORT.md`, `.aris/edit_whitelist.yaml` | `ls` | missing → `FAIL` |
| S5 | Hard constraints held: `PAPER_IMPROVEMENT_LOG.md` has no edits outside the whitelist; `.bib` identical to the source (bib frozen); no new result files | diff `.bib` hashes; `Grep rejected_by_edit_whitelist` | changed bib → `FAIL` |
| S6 | Page count within the new venue limit (from `COMPILE_REPORT.json`) | parse | over → `FAIL` |
| S7 | Every audit verdict ∈ {PASS, NOT_APPLICABLE} for convergence | read the four audit JSONs | WARN → `WARN`; FAIL/BLOCKED/ERROR → `FAIL` |
| S8 | `.aris/forensics/gate.json` OPEN obligations are copied verbatim into the report | compare | missing → `WARN` |

## Step 2C: W6 talk (`slides/`, `.aris/paper-talk/`)

| # | Requirement | How to check | Fail state |
|---|-------------|--------------|------------|
| T1 | `slides/SLIDE_OUTLINE.md`, `main.tex`, `main.pdf`, `presentation.pptx`, `speaker_notes.md`, `TALK_SCRIPT.md` exist | `ls` | `FAIL` |
| T2 | `.aris/paper-talk/PIPELINE_STATE.json` shows phases 0–5 complete; `FINAL_REPORT.md` verdict recorded | parse | incomplete → `FAIL` |
| T3 | Audit JSONs in `.aris/paper-talk/audits/` (`slide_claim_audit`, `citation_audit`, `anonymity_scan`, `export_integrity`) use the 6-state schema | parse | missing → `WARN` (draft) / `FAIL` (submission) |
| T4 | Export integrity: PPTX-PDF page count == Beamer page count; 16:9; no blank slide; notes preserved | `export_integrity.json` fields | any false → `FAIL` |
| T5 | If `presentation_polished.pptx` exists: `/slides-polish` `TRIAGE.md` has no `BLOCKER` slide, `POLISH_CHANGELOG.md` exists, original deck unchanged | `Grep BLOCKER` | `BLOCKER` → `FAIL` |
| T6 | `anonymity_scan` PASS when the venue is double-blind | parse | FAIL → `FAIL` |
| T7 | Deck newer than the paper PDF it was built from | mtimes | older → `WARN` (`deck_stale`) |

## Step 3: Report

Write `.aris/inspections/post-paper_<target>_<ts>.{md,json}` and fixed-name copies
(`post-paper_<target>.{md,json}`); `inspected_files` = every file read above.

**Next step** by verdict:
- Rebuttal `PASS` → paste `PASTE_READY.txt` (or the per-reviewer files) into the venue system.
- Rebuttal `FAIL` on R5–R10 → `/rebuttal` (resume from `REBUTTAL_STATE.md`).
- Resubmit `FAIL` → `/resubmit-pipeline` (resume); never edit the source paper dir.
- Talk `FAIL` → `/paper-talk` (resume) or `/slides-polish`.

## Key Rules

- Never edit `PASTE_READY.txt`, rebuttal drafts, resubmit outputs, or slide files — not even to trim characters.
- Character counts and page counts are measured, never estimated.
- A source-dir hash mismatch in W5 is always `FAIL`, regardless of assurance.
