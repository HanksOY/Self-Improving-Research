---
name: hparam-sweep
description: "Automatic hyperparameter search for ML training with Optuna (resumable SQLite study, multi-GPU workers, median/Hyperband pruning) or W&B Sweeps, driven by one JSON spec. Selects on a validation metric only, re-runs the best config over several seeds, and writes a sweep report with parameter importance and search-edge warnings. Use when user says \"hyperparameter search\", \"hparam sweep\", \"tune hyperparameters\", \"optuna\", \"wandb sweep\", \"调参\", \"超参搜索\"."
argument-hint: "[train-command-or-script] [— backend: optuna|wandb, trials: N, gpus: 0,1, metric: val_acc, direction: maximize|minimize, seeds: 3]"
allowed-tools: Bash(*), Read, Write, Edit, Grep, Glob
---

# Hyperparameter Sweep

Sweep: $ARGUMENTS

## Role & Position

| Skill | Job |
|-------|-----|
| `/experiment-plan` | Decides *which* hyperparameters matter and the budget |
| **`/hparam-sweep`** | Searches them automatically, then confirms the best config over seeds |
| `/run-experiment`, `/vast-gpu`, `/serverless-modal`, `/qzcli` | Provide the GPU machine; this skill runs its driver there |
| `/training-check` | Watches individual runs for NaN / divergence |
| `/checkpoint-eval` | Picks the best checkpoint *inside* the winning run |
| `/ablation-planner` | Component ablations take priority over wide sweeps |

Follow [`experiment-integrity.md`](../shared-references/experiment-integrity.md):
the objective is a **validation** metric. The test split is never in the search loop.

## Constants

- **BACKEND = optuna** — `— backend: wandb` writes a W&B sweep config instead.
- **N_TRIALS = 40**, **TRIAL_TIMEOUT = 120 min**, **SWEEP_TIMEOUT = 12 h** — from `— trials:` or the spec.
- **SAMPLER = tpe**, **PRUNER = median** (`hyperband` for long runs with many epochs).
- **CONFIRM_SEEDS = 3** — re-run the best config with this many new seeds.
- **SPEC** — `sweeps/<name>/sweep_spec.json`.

## Workflow

### Step 1: Locate the driver

```bash
# Layer 0: self-contained (CC 1.0+ exposes $CLAUDE_SKILL_DIR).
SWEEP_DRIVER=""
if [ -n "${CLAUDE_SKILL_DIR:-}" ] && [ -f "$CLAUDE_SKILL_DIR/scripts/hparam_sweep.py" ]; then
  SWEEP_DRIVER="$CLAUDE_SKILL_DIR/scripts/hparam_sweep.py"
fi
# Layers 1-4: repo checkout / global pointer (non-CC hosts, copy installs).
if [ -z "$SWEEP_DRIVER" ]; then
  if [ -z "${ARIS_REPO:-}" ] && [ -f .aris/installed-skills.txt ]; then
      ARIS_REPO=$(awk -F'\t' '$1=="repo_root"{print $2; exit}' .aris/installed-skills.txt 2>/dev/null) || true
  fi
  if [ -z "${ARIS_REPO:-}" ] && [ -f "$HOME/.aris/repo" ]; then
      ARIS_REPO=$(cat "$HOME/.aris/repo" 2>/dev/null) || true
  fi
  [ -n "${ARIS_REPO:-}" ] && [ -f "$ARIS_REPO/skills/hparam-sweep/scripts/hparam_sweep.py" ] && \
    SWEEP_DRIVER="$ARIS_REPO/skills/hparam-sweep/scripts/hparam_sweep.py"
  [ -z "$SWEEP_DRIVER" ] && [ -f "$HOME/.claude/skills/hparam-sweep/scripts/hparam_sweep.py" ] && \
    SWEEP_DRIVER="$HOME/.claude/skills/hparam-sweep/scripts/hparam_sweep.py"
fi
[ -z "$SWEEP_DRIVER" ] && {
  echo "ERROR: hparam_sweep.py not resolved (\$CLAUDE_SKILL_DIR/scripts/, \$ARIS_REPO/skills/hparam-sweep/scripts/, ~/.claude/skills/hparam-sweep/scripts/)." >&2
  echo "       Fix: rerun bash tools/install_aris.sh or smart_update.sh, or export ARIS_REPO." >&2
  exit 1
}
PY=$(command -v python3 || command -v python)
```

The driver runs where the GPUs are. For a remote machine, sync it with the code
(`/run-experiment` Step 3) and run Steps 4–5 there inside `screen` / `tmux`.

### Step 2: Make the training script sweep-ready

The driver needs three things from the training script. Check them with `Grep`, and add them with `Edit` if they are missing:

1. **Arguments** for every swept parameter, plus `--seed` and an output folder (`--out`).
2. **Final metrics file**: `<out>/metrics.json` with the validation metric, for example `{"val_acc": 0.91}`. Write it only after evaluation ends.
3. **Progress file** (needed for pruning): append one line per evaluation to `<out>/progress.jsonl`, for example `{"step": 3, "value": 0.87}`. Use the same metric and direction.

For W&B, the script instead reads `wandb.config` (or `--name=value` arguments) and calls `wandb.log({"val_acc": ...})`.

### Step 3: Write the spec

```bash
"$PY" "$SWEEP_DRIVER" init --out sweeps/<name>/sweep_spec.json
```

Edit the spec: `command` (with `{param}`, `{seed}`, `{trial_dir}` placeholders),
`metric`, `params`, `fixed`, `budget`, `sampler`, `pruner`. Search-space rules:

| Parameter | Type and scale |
|-----------|----------------|
| learning rate, weight decay, ε, temperature | `float`, `log: true` (1e-5 to 1e-2 is a typical LR range) |
| dropout, momentum, warmup ratio | `float`, linear |
| layers, heads, hidden size | `int` or `categorical` |
| optimizer, scheduler, batch size | `categorical` |

Keep ≤ 6 swept parameters. Fix everything else in `fixed`. Then validate:

```bash
"$PY" "$SWEEP_DRIVER" validate sweeps/<name>/sweep_spec.json
```

It rejects unknown placeholders, bad ranges and a metric whose name contains `test`.

**Budget check:** `N_TRIALS × typical trial time ÷ workers` must fit `SWEEP_TIMEOUT`.
Show the estimate to the user. If it is over 24 GPU-hours, wait for confirmation,
unless `AUTO_PROCEED` is true and the user already approved the budget in `EXPERIMENT_PLAN.md`.

### Step 4: Run

**Optuna** (resumable — rerun the same command after a crash):

```bash
"$PY" "$SWEEP_DRIVER" optuna sweeps/<name>/sweep_spec.json --gpus 0,1,2,3
```

One worker per GPU (`CUDA_VISIBLE_DEVICES` is set per trial). Each trial writes
`trials/trial_NNNN/{train.log,progress.jsonl,metrics.json}`. A crashed or timed-out
trial is recorded as `FAIL`, and the study continues.

**W&B Sweeps:**

```bash
"$PY" "$SWEEP_DRIVER" wandb-config sweeps/<name>/sweep_spec.json --out sweeps/<name>/sweep.yaml
wandb sweep --project <project> sweeps/<name>/sweep.yaml     # prints the sweep id
wandb agent <entity>/<project>/<sweep_id> --count <N_TRIALS>  # one agent per GPU
```

While it runs: check `sweep_results.csv` (Optuna: `summarize`) every 30–60 min.
Stop early and tell the user if more than 30% of the trials fail. That points to a bug, not to bad hyperparameters.

### Step 5: Summarize and confirm

```bash
"$PY" "$SWEEP_DRIVER" summarize sweeps/<name>/sweep_spec.json
for s in 101 102 103; do "$PY" "$SWEEP_DRIVER" best-cmd sweeps/<name>/sweep_spec.json --seed $s; done
```

Run each printed confirmation command (via `/run-experiment` or directly). Report the
**mean ± std over the confirmation seeds**, not the single best trial: the best trial
is biased upward by selection.

`best_at_search_edge` lists parameters whose best value lies at the edge of the range.
For each one, widen the range and run a short follow-up sweep, or say in the report that the true optimum may lie outside the range.

### Step 6: Report

Write `sweeps/<name>/SWEEP_REPORT.md`:

```markdown
# Sweep: <name>
- Backend / sampler / pruner: optuna / tpe / median · trials: 40 (31 complete, 7 pruned, 2 failed)
- Objective: val_acc (maximize) · GPU-hours used: <n>
## Best config
| param | value | range |
## Confirmation (seeds 101–103): val_acc = 0.912 ± 0.004
## Parameter importance
## Search-edge warnings
## Failed trials (cause from train.log)
```

Add one row per confirmation run to `refine-logs/EXPERIMENT_TRACKER.md`, and add an
`EXPERIMENT_LOG.md` entry if those files exist. Hand off: `/checkpoint-eval` for the
winning run, then `/result-to-claim`.

## Key Rules

- Select on validation only. Evaluate test once, after the sweep, on the confirmed config (`/checkpoint-eval test`).
- Never report the best single trial as the result; report the confirmation seeds.
- Never change the search space or metric in the middle of a study; start a new study name instead.
- Same data split, same eval code and same seed policy for every trial. A change means a new study.
- Record the full spec, the driver output and the study database; they are the evidence for the paper's hyperparameter table.
