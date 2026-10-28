#!/usr/bin/env python3
"""Hyperparameter sweep driver for /hparam-sweep (Optuna or W&B Sweeps).

One JSON spec drives both backends. The training command is a template; the
driver fills {param} placeholders, runs it, and reads the objective from a
metrics JSON file the training script writes. Optional progress lines
(JSONL: {"step": int, "value": float}) enable pruning.

Commands:
  init --out SPEC.json            write a commented starter spec
  validate SPEC.json              check the spec (stdlib only)
  optuna SPEC.json [--gpus 0,1]   run / resume the Optuna study (needs `optuna`)
  wandb-config SPEC.json --out F  write a W&B sweep YAML (stdlib only)
  summarize SPEC.json             write trials CSV + summary JSON (needs `optuna`)
  best-cmd SPEC.json --seed S     print the command for the best params with seed S

Exit codes: 0 ok, 1 spec invalid / no completed trials, 2 missing dependency or file.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import shlex
import subprocess
import sys
import time
from pathlib import Path

TYPES = {"float", "int", "categorical"}
STARTER = {
    "name": "my_sweep",
    "command": "python train.py --lr {lr} --weight-decay {weight_decay} --batch-size {batch_size} "
               "--seed {seed} --out {trial_dir}",
    "workdir": ".",
    "trial_root": "sweeps/my_sweep/trials",
    "storage": "sqlite:///sweeps/my_sweep/study.db",
    "metric": {"name": "val_acc", "direction": "maximize",
               "file": "{trial_dir}/metrics.json", "progress_file": "{trial_dir}/progress.jsonl"},
    "params": {
        "lr": {"type": "float", "low": 1e-5, "high": 1e-2, "log": True},
        "weight_decay": {"type": "float", "low": 0.0, "high": 0.1},
        "batch_size": {"type": "categorical", "choices": [32, 64, 128]},
    },
    "fixed": {"seed": 0},
    "budget": {"n_trials": 40, "timeout_hours": 12, "trial_timeout_minutes": 120},
    "sampler": "tpe",
    "pruner": "median",
    "wandb": {"project": "my-project", "method": "bayes", "program": "train.py"},
}


# ---------------------------------------------------------------- spec

def load_spec(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"ERROR: spec {path} not found", file=sys.stderr)
        sys.exit(2)
    except ValueError as exc:
        print(f"ERROR: spec is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)


def spec_errors(spec: dict) -> list[str]:
    errs = []
    for key in ("name", "command", "metric", "params", "budget"):
        if key not in spec:
            errs.append(f"missing key: {key}")
    metric = spec.get("metric", {})
    if metric.get("direction") not in {"maximize", "minimize"}:
        errs.append("metric.direction must be maximize or minimize")
    for key in ("name", "file"):
        if not metric.get(key):
            errs.append(f"metric.{key} is required")
    if "test" in str(metric.get("name", "")).lower():
        errs.append("metric.name looks like a TEST metric; select on validation only (experiment-integrity)")
    params = spec.get("params", {})
    if not params:
        errs.append("params is empty")
    for name, p in params.items():
        t = p.get("type")
        if t not in TYPES:
            errs.append(f"params.{name}.type must be one of {sorted(TYPES)}")
        elif t == "categorical":
            if not p.get("choices"):
                errs.append(f"params.{name}.choices is empty")
        else:
            if "low" not in p or "high" not in p or not p["low"] < p["high"]:
                errs.append(f"params.{name} needs low < high")
            if p.get("log") and p.get("low", 0) <= 0:
                errs.append(f"params.{name}: log scale needs low > 0")
    names = set(params) | set(spec.get("fixed", {})) | {"trial_dir", "trial_id", "gpu"}
    for field in (spec.get("command", ""), metric.get("file", ""), metric.get("progress_file", "")):
        try:
            field.format_map({n: "x" for n in names})
        except KeyError as exc:
            errs.append(f"placeholder {exc} in '{field}' is not a param, fixed value or built-in")
        except (ValueError, IndexError) as exc:
            errs.append(f"bad placeholder syntax in '{field}': {exc} (use {{{{ }}}} for literal braces)")
    budget = spec.get("budget", {})
    if not isinstance(budget.get("n_trials"), int) or budget.get("n_trials", 0) < 1:
        errs.append("budget.n_trials must be a positive integer")
    if spec.get("pruner", "none") not in {"none", "median", "hyperband", "successive_halving"}:
        errs.append("pruner must be none, median, hyperband or successive_halving")
    if spec.get("sampler", "tpe") not in {"tpe", "random", "cmaes", "grid"}:
        errs.append("sampler must be tpe, random, cmaes or grid")
    return errs


def _fill(template: str, values: dict) -> str:
    return template.format_map(values)


# ---------------------------------------------------------------- optuna backend

def _optuna():
    try:
        import optuna  # noqa: F401
        return optuna
    except ImportError:
        print("ERROR: optuna is not installed. Fix: pip install optuna", file=sys.stderr)
        sys.exit(2)


def _sampler(optuna, spec):
    kind = spec.get("sampler", "tpe")
    seed = spec.get("sampler_seed", 0)
    if kind == "random":
        return optuna.samplers.RandomSampler(seed=seed)
    if kind == "cmaes":
        return optuna.samplers.CmaEsSampler(seed=seed)
    if kind == "grid":
        grid = {}
        for name, p in spec["params"].items():
            if p["type"] != "categorical":
                print(f"ERROR: grid sampler needs categorical params ({name} is {p['type']})", file=sys.stderr)
                sys.exit(1)
            grid[name] = p["choices"]
        return optuna.samplers.GridSampler(grid, seed=seed)
    return optuna.samplers.TPESampler(seed=seed, multivariate=True)


def _pruner(optuna, spec):
    kind = spec.get("pruner", "none")
    if kind == "median":
        return optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=spec.get("warmup_steps", 0))
    if kind == "hyperband":
        return optuna.pruners.HyperbandPruner()
    if kind == "successive_halving":
        return optuna.pruners.SuccessiveHalvingPruner()
    return optuna.pruners.NopPruner()


def _suggest(trial, spec) -> dict:
    values = {}
    for name, p in spec["params"].items():
        if p["type"] == "float":
            values[name] = trial.suggest_float(name, p["low"], p["high"], log=bool(p.get("log")),
                                               step=p.get("step"))
        elif p["type"] == "int":
            values[name] = trial.suggest_int(name, p["low"], p["high"], log=bool(p.get("log")),
                                             step=p.get("step", 1))
        else:
            values[name] = trial.suggest_categorical(name, p["choices"])
    return values


def _read_progress(path: Path, offset: int) -> tuple[list[tuple[int, float]], int]:
    if not path.is_file():
        return [], offset
    out = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        for line in fh:
            if not line.endswith("\n"):
                break
            offset += len(line.encode("utf-8"))
            try:
                rec = json.loads(line)
                out.append((int(rec["step"]), float(rec["value"])))
            except (ValueError, KeyError, TypeError):
                continue
    return out, offset


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def make_objective(optuna, spec, gpus: "queue.Queue[str]"):
    metric = spec["metric"]
    root = Path(spec.get("trial_root", f"sweeps/{spec['name']}/trials"))
    trial_timeout = 60 * float(spec["budget"].get("trial_timeout_minutes", 0) or 0)
    poll = float(spec.get("poll_seconds", 10))

    def objective(trial):
        values = {**spec.get("fixed", {}), **_suggest(trial, spec)}
        trial_dir = root / f"trial_{trial.number:04d}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        gpu = gpus.get()
        try:
            values.update(trial_dir=trial_dir.as_posix(), trial_id=trial.number, gpu=gpu)
            cmd = _fill(spec["command"], values)
            trial.set_user_attr("command", cmd)
            env = dict(os.environ)
            if gpu != "":
                env["CUDA_VISIBLE_DEVICES"] = gpu
            metrics_file = Path(_fill(metric["file"], values))
            progress = Path(_fill(metric.get("progress_file", "{trial_dir}/progress.jsonl"), values))
            start, offset = time.time(), 0
            with (trial_dir / "train.log").open("w", encoding="utf-8") as log:
                proc = subprocess.Popen(cmd, shell=True, cwd=spec.get("workdir", "."), env=env,
                                        stdout=log, stderr=subprocess.STDOUT)
                while proc.poll() is None:
                    time.sleep(poll)
                    points, offset = _read_progress(progress, offset)
                    for step, value in points:
                        trial.report(value, step)
                    if points and trial.should_prune():
                        _stop(proc)
                        raise optuna.TrialPruned(f"pruned at step {points[-1][0]}")
                    if trial_timeout and time.time() - start > trial_timeout:
                        _stop(proc)
                        raise RuntimeError(f"trial timeout after {trial_timeout / 60:.0f} min")
            points, _ = _read_progress(progress, offset)
            for step, value in points:
                trial.report(value, step)
            if proc.returncode != 0:
                raise RuntimeError(f"command exited {proc.returncode}; see {trial_dir / 'train.log'}")
            try:
                data = json.loads(metrics_file.read_text(encoding="utf-8"))
                value = float(data[metric["name"]])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f"metric '{metric['name']}' not readable from {metrics_file}: {exc}")
            if value != value:  # NaN
                raise RuntimeError("metric is NaN")
            trial.set_user_attr("metrics", data)
            return value
        finally:
            gpus.put(gpu)

    return objective


def cmd_optuna(args) -> int:
    spec = load_spec(args.spec)
    errs = spec_errors(spec)
    if errs:
        print("Spec invalid:\n- " + "\n- ".join(errs), file=sys.stderr)
        return 1
    optuna = _optuna()
    storage = spec.get("storage", f"sqlite:///sweeps/{spec['name']}/study.db")
    if storage.startswith("sqlite:///"):
        Path(storage[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
    study = optuna.create_study(study_name=spec["name"], storage=storage, load_if_exists=True,
                                direction=spec["metric"]["direction"],
                                sampler=_sampler(optuna, spec), pruner=_pruner(optuna, spec))
    done = len([t for t in study.trials if t.state.is_finished()])
    total = args.n_trials or spec["budget"]["n_trials"]
    remaining = max(0, total - done)
    if remaining == 0:
        print(f"Study '{spec['name']}' already has {done} finished trials (budget {total}).")
        return 0
    gpu_list = [g.strip() for g in (args.gpus or spec.get("gpus", "")).split(",")] if (args.gpus or spec.get("gpus")) else [""]
    gpus: "queue.Queue[str]" = queue.Queue()
    for g in gpu_list:
        gpus.put(g)
    timeout = float(spec["budget"].get("timeout_hours", 0) or 0) * 3600 or None
    print(f"Running {remaining} trial(s) on {len(gpu_list)} worker(s); storage {storage}")
    study.optimize(make_objective(optuna, spec, gpus), n_trials=remaining, timeout=timeout,
                   n_jobs=len(gpu_list), catch=(RuntimeError,), gc_after_trial=True)
    return write_summary(study, spec)


# ---------------------------------------------------------------- summary

def write_summary(study, spec) -> int:
    out_dir = Path(spec.get("trial_root", f"sweeps/{spec['name']}/trials")).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    names = list(spec["params"])
    rows = []
    for t in study.trials:
        rows.append({"number": t.number, "state": t.state.name,
                     "value": "" if t.value is None else t.value,
                     **{n: t.params.get(n, "") for n in names},
                     "duration_s": round(t.duration.total_seconds(), 1) if t.duration else ""})
    with (out_dir / "sweep_results.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["number", "state", "value", *names, "duration_s"])
        writer.writeheader()
        writer.writerows(rows)
    states: dict[str, int] = {}
    for t in study.trials:
        states[t.state.name] = states.get(t.state.name, 0) + 1
    complete = [t for t in study.trials if t.state.name == "COMPLETE"]
    summary = {"study": spec["name"], "metric": spec["metric"]["name"],
               "direction": spec["metric"]["direction"], "states": states}
    if complete:
        best = study.best_trial
        summary.update(best_number=best.number, best_value=best.value, best_params=best.params,
                       best_command=best.user_attrs.get("command", ""))
        try:
            import optuna
            if len(complete) >= 4 and len(names) > 1:
                summary["param_importance"] = {
                    k: round(v, 4) for k, v in optuna.importance.get_param_importances(study).items()}
        except Exception as exc:  # importance is optional
            summary["param_importance_error"] = str(exc)
        ordered = sorted(complete, key=lambda t: t.value, reverse=spec["metric"]["direction"] == "maximize")
        summary["top"] = [{"number": t.number, "value": t.value, "params": t.params} for t in ordered[:5]]
        edges = []
        for n, p in spec["params"].items():
            if p["type"] in {"float", "int"} and n in best.params:
                span = p["high"] - p["low"]
                if best.params[n] <= p["low"] + 0.05 * span or best.params[n] >= p["high"] - 0.05 * span:
                    edges.append(n)
        summary["best_at_search_edge"] = edges
    (out_dir / "sweep_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary.get(k) for k in ("states", "best_value", "best_params", "best_at_search_edge")}))
    print(f"Wrote {out_dir / 'sweep_results.csv'} and {out_dir / 'sweep_summary.json'}")
    return 0 if complete else 1


def _load_study(spec):
    optuna = _optuna()
    storage = spec.get("storage", f"sqlite:///sweeps/{spec['name']}/study.db")
    try:
        return optuna.load_study(study_name=spec["name"], storage=storage)
    except KeyError:
        print(f"ERROR: no study '{spec['name']}' in {storage}", file=sys.stderr)
        sys.exit(2)


def cmd_summarize(args) -> int:
    spec = load_spec(args.spec)
    return write_summary(_load_study(spec), spec)


def cmd_best_cmd(args) -> int:
    spec = load_spec(args.spec)
    study = _load_study(spec)
    try:
        best = study.best_trial
    except ValueError:
        print("ERROR: no completed trials", file=sys.stderr)
        return 1
    root = Path(spec.get("trial_root", f"sweeps/{spec['name']}/trials")).parent / "confirm"
    values = {**spec.get("fixed", {}), **best.params, "seed": args.seed,
              "trial_dir": (root / f"seed_{args.seed}").as_posix(), "trial_id": f"best_s{args.seed}", "gpu": ""}
    print(_fill(spec["command"], values))
    return 0


# ---------------------------------------------------------------- wandb backend

def _yaml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v))


def cmd_wandb_config(args) -> int:
    spec = load_spec(args.spec)
    errs = spec_errors(spec)
    if errs:
        print("Spec invalid:\n- " + "\n- ".join(errs), file=sys.stderr)
        return 1
    wb = spec.get("wandb", {})
    method = wb.get("method", "bayes")
    goal = "maximize" if spec["metric"]["direction"] == "maximize" else "minimize"
    lines = [f"# Generated by hparam_sweep.py from {Path(args.spec).name}",
             f"name: {_yaml_scalar(spec['name'])}", f"method: {method}",
             f"program: {_yaml_scalar(wb.get('program', 'train.py'))}",
             "metric:", f"  name: {_yaml_scalar(spec['metric']['name'])}", f"  goal: {goal}", "parameters:"]
    for name, p in spec["params"].items():
        lines.append(f"  {name}:")
        if p["type"] == "categorical":
            lines.append("    values: [" + ", ".join(_yaml_scalar(c) for c in p["choices"]) + "]")
        elif p["type"] == "float":
            dist = "log_uniform_values" if p.get("log") else "uniform"
            lines += [f"    distribution: {dist}", f"    min: {_yaml_scalar(float(p['low']))}",
                      f"    max: {_yaml_scalar(float(p['high']))}"]
        else:
            dist = "q_log_uniform_values" if p.get("log") else "int_uniform"
            lines += [f"    distribution: {dist}", f"    min: {int(p['low'])}", f"    max: {int(p['high'])}"]
    for name, value in spec.get("fixed", {}).items():
        lines += [f"  {name}:", f"    value: {_yaml_scalar(value)}"]
    if spec.get("pruner", "none") != "none":
        lines += ["early_terminate:", "  type: hyperband", f"  min_iter: {int(wb.get('min_iter', 3))}"]
    lines += ["command:", "  - ${env}", "  - ${interpreter}", "  - ${program}", "  - ${args}"]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    project = wb.get("project", "<project>")
    print(f"Wrote {args.out}")
    print(f"Next: wandb sweep --project {shlex.quote(project)} {shlex.quote(args.out)}  "
          f"then: wandb agent <entity>/{project}/<sweep_id> --count {spec['budget']['n_trials']}")
    return 0


# ---------------------------------------------------------------- misc commands

def cmd_init(args) -> int:
    out = Path(args.out)
    if out.exists():
        print(f"ERROR: {out} exists; not overwriting", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(STARTER, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote starter spec {out}")
    return 0


def cmd_validate(args) -> int:
    errs = spec_errors(load_spec(args.spec))
    if errs:
        print("INVALID\n- " + "\n- ".join(errs))
        return 1
    print("VALID")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--out", required=True)
    for name in ("validate", "optuna", "wandb-config", "summarize", "best-cmd"):
        p = sub.add_parser(name)
        p.add_argument("spec")
        if name == "optuna":
            p.add_argument("--gpus", help="comma-separated GPU ids, one worker per GPU")
            p.add_argument("--n-trials", type=int, help="override budget.n_trials")
        if name == "wandb-config":
            p.add_argument("--out", required=True)
        if name == "best-cmd":
            p.add_argument("--seed", type=int, required=True)
    args = ap.parse_args()
    return {"init": cmd_init, "validate": cmd_validate, "optuna": cmd_optuna, "wandb-config": cmd_wandb_config,
            "summarize": cmd_summarize, "best-cmd": cmd_best_cmd}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
