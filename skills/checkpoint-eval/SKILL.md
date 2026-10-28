---
name: checkpoint-eval
description: "Manage training checkpoints and their evaluation: scan run folders into a hashed registry, evaluate every checkpoint on a validation split with your eval command, select the best on validation only, run the test split exactly once on the selected checkpoint, verify that evaluated weights have not changed, and prune old checkpoints with a dry-run-first retention policy (best-K, last-N, pinned, selected are always kept). Use when user says \"evaluate checkpoints\", \"pick best checkpoint\", \"clean up checkpoints\", \"checkpoint management\", \"选 checkpoint\", \"评测模型\"."
argument-hint: "[run-dir...] [— metric: val_acc, direction: max|min, eval-cmd: \"...\", keep-best: 3, keep-last: 2, baseline: <ckpt-or-model>]"
allowed-tools: Bash(*), Read, Write, Edit, Grep, Glob
---

# Checkpoint Management & Evaluation

Checkpoints: $ARGUMENTS

## Role & Position

| Skill | Job |
|-------|-----|
| `/run-experiment`, `/llm-finetune`, `/hparam-sweep` | Produce runs and checkpoints |
| **`/checkpoint-eval`** | Registry, validation eval, selection, one-time test eval, retention |
| `/analyze-results` | Statistics and tables across runs |
| `/result-to-claim` | Decides whether the selected result supports a claim |
| `/paper-claim-audit` | Later checks the paper numbers against the eval JSON this skill keeps |

Follow [`experiment-integrity.md`](../shared-references/experiment-integrity.md):
**select on validation, report on test, and evaluate test once.**

## Constants

- **REGISTRY = `checkpoints/CHECKPOINT_REGISTRY.json`** (eval outputs go to `checkpoints/evals/<split>/`).
- **METRIC / DIRECTION** — from `— metric:` / `— direction:`; else from `EXPERIMENT_PLAN.md`; else ask.
- **KEEP_BEST = 3**, **KEEP_LAST = 2**, **KEEP_EVERY = 0** (for example `10000` keeps every 10k-step milestone).
- **BASELINE** — the reference to evaluate with the same command, for example the base model for `/llm-finetune` or the previous best method.

## Workflow

### Step 1: Locate the helper

```bash
# Layer 0: self-contained (CC 1.0+ exposes $CLAUDE_SKILL_DIR).
CKPT_TOOL=""
if [ -n "${CLAUDE_SKILL_DIR:-}" ] && [ -f "$CLAUDE_SKILL_DIR/scripts/ckpt.py" ]; then
  CKPT_TOOL="$CLAUDE_SKILL_DIR/scripts/ckpt.py"
fi
# Layers 1-4: repo checkout / global pointer (non-CC hosts, copy installs).
if [ -z "$CKPT_TOOL" ]; then
  if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
      ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
  fi
  if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
      ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
  fi
  [ -n "${ARIS_REPO:-}" ] && [ -f "$ARIS_REPO/skills/checkpoint-eval/scripts/ckpt.py" ] && \
    CKPT_TOOL="$ARIS_REPO/skills/checkpoint-eval/scripts/ckpt.py"
  [ -z "$CKPT_TOOL" ] && [ -f "$HOME/.claude/skills/checkpoint-eval/scripts/ckpt.py" ] && \
    CKPT_TOOL="$HOME/.claude/skills/checkpoint-eval/scripts/ckpt.py"
fi
[ -z "$CKPT_TOOL" ] && {
  echo "ERROR: ckpt.py not resolved (\$CLAUDE_SKILL_DIR/scripts/, \$ARIS_REPO/skills/checkpoint-eval/scripts/, ~/.claude/skills/checkpoint-eval/scripts/)." >&2
  echo "       Fix: rerun bash tools/install_aris.sh or smart_update.sh, or export ARIS_REPO." >&2
  exit 1
}
PY=$(command -v python3 || command -v python)
```

Run the helper on the machine that holds the checkpoints. The registry stores paths relative to the folder where you run it.

### Step 2: Scan

```bash
"$PY" "$CKPT_TOOL" scan runs/<run-a> runs/<run-b>
```

The scan finds `*.pt / *.pth / *.ckpt / *.safetensors / *.bin` files and Hugging Face
checkpoint folders (`checkpoint-N/`, LoRA adapter folders). Optimizer state is skipped.
Each checkpoint gets a **quick hash** (size plus the first and last 4 MiB of each file).
A rescan of changed weights clears their old evaluations.

### Step 3: Write or find the eval command

The eval command is a template with `{ckpt}`, `{out}` and `{split}` placeholders. It must write a JSON
file of metrics to `{out}`:

```bash
python eval.py --ckpt {ckpt} --split {split} --out {out}            # your project's script
# LLMs, for example with lm-evaluation-harness (check its CLI version first):
# lm_eval --model hf --model_args pretrained={ckpt} --tasks <task> --output_path {out}.d && <convert to {out}>
```

If the project has no eval script, write `eval.py`. It loads the checkpoint, runs the
split with a **fixed seed and fixed decoding settings**, and writes `{"<metric>": value, ...}`.
Use the same script for every checkpoint and for the baseline.

### Step 4: Evaluate on validation

```bash
"$PY" "$CKPT_TOOL" eval --cmd "python eval.py --ckpt {ckpt} --split {split} --out {out}" --metric val_acc --split val
"$PY" "$CKPT_TOOL" eval --cmd "..." --metric val_acc --split val --only-new      # later, for new checkpoints only
```

Each result stores the command, the exit code, the time and the weights' quick hash. A failed eval is
recorded as `ok: false`, and it is never used for selection. The helper refuses `--split test` here.

Evaluate the **baseline** with the same command, and record it in the report.

### Step 5: Select

```bash
"$PY" "$CKPT_TOOL" select --metric val_acc --direction max --split val
```

Selection uses only evaluations whose quick hash still matches the weights. If two
checkpoints are within the validation noise (see `/analyze-results`), prefer the earlier one and say so.

### Step 6: Test once

```bash
"$PY" "$CKPT_TOOL" test --cmd "python eval.py --ckpt {ckpt} --split {split} --out {out}" --metric test_acc
```

The test split runs only on the selected checkpoint, and only once. A second run needs
`--force --reason "<why>"`. The reason is recorded and shown in the report. Re-running test
after a change in selection is test-set tuning, so tell the user if anyone asks for it.

### Step 7: Verify and prune

```bash
"$PY" "$CKPT_TOOL" verify                                          # missing or changed weights -> exit 1
"$PY" "$CKPT_TOOL" pin runs/a/step_12000.pt --reason "Figure 3"    # anything a paper or plot uses
"$PY" "$CKPT_TOOL" prune --keep-best 3 --keep-last 2               # DRY RUN: prints KEEP / WOULD DELETE
```

**Always show the dry-run list and the freed size to the user, and get an explicit yes
before running `--apply`.** `AUTO_PROCEED` does not cover deletion. The prune never deletes a checkpoint that is
the selected one, pinned, among the best K or last N, a `--keep-every` milestone, or one that has a test result.

### Step 8: Report

```bash
"$PY" "$CKPT_TOOL" report --out checkpoints/CHECKPOINTS.md
```

Add a summary for the user, and add it to `refine-logs/EXPERIMENT_LOG.md` if that file exists:

```markdown
## Checkpoint selection — <run>
- Candidates: 14 evaluated on val (2 failed evals: <paths>)
- Selected: runs/a/checkpoint-6000 · val_acc 0.912 (baseline 0.874)
- Test (run once): test_acc 0.905 · eval JSON: checkpoints/evals/test/<file>.json
- Kept: 5 · deleted: 9 (31.2 GB) · pinned: Figure 3 checkpoint
```

Hand off to `/result-to-claim` with the eval JSON paths as the evidence.

## Key Rules

- Never select on test. Never run test before `select`.
- Never delete without a dry run and the user's confirmation. Never delete pinned, selected, or test-evaluated checkpoints.
- Every reported number must trace to an eval JSON in `checkpoints/evals/`. The paper audits rely on it.
- Evaluate the baseline and every checkpoint with the same command, data and decoding settings.
- If `verify` reports changed weights, re-evaluate before you report any number from them.
