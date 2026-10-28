#!/usr/bin/env python3
"""Checkpoint registry, evaluation and retention for /checkpoint-eval (standard library only).

The registry (default checkpoints/CHECKPOINT_REGISTRY.json) records every
checkpoint, its quick hash, step and per-split metrics. Selection uses a
validation split only; the test split runs once, on the selected checkpoint.

Commands:
  scan ROOTS... [--registry R]                  find checkpoints and add / update them
  eval --cmd TEMPLATE --metric M [--split val] [--only-new] [--registry R]
                                                run the eval command per checkpoint ({ckpt}, {out}, {split})
  select --metric M --direction max|min [--split val]   mark the best checkpoint
  pin PATH --reason TEXT                        protect a checkpoint from pruning
  prune --keep-best K --keep-last L [--keep-every N] [--apply]   dry run unless --apply
  test --cmd TEMPLATE --metric M [--force]      evaluate the selected checkpoint on the test split, once
  verify                                        re-hash and flag changed / missing checkpoints
  report [--out CHECKPOINTS.md]                 Markdown table of the registry

Exit codes: 0 ok, 1 nothing to do / check failed, 2 bad arguments or registry.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

FILE_EXT = {".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".pkl", ".npz", ".h5", ".msgpack"}
HF_MARKERS = {"adapter_config.json", "config.json", "model.safetensors", "pytorch_model.bin",
              "model.safetensors.index.json", "adapter_model.safetensors"}
STEP_RE = re.compile(r"(?:checkpoint|step|iter|global_step)[-_]?(\d+)", re.I)
EPOCH_RE = re.compile(r"epoch[-_=]?(\d+)", re.I)
CHUNK = 4 * 1024 * 1024


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def size_of(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def quick_hash(p: Path) -> str:
    """size + sha256 of the first and last 4 MiB of every file (fast on multi-GB weights)."""
    h = hashlib.sha256()
    files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file())
    for f in files:
        size = f.stat().st_size
        h.update(f"{f.name}:{size}".encode())
        with f.open("rb") as fh:
            h.update(fh.read(CHUNK))
            if size > 2 * CHUNK:
                fh.seek(-CHUNK, os.SEEK_END)
                h.update(fh.read(CHUNK))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- registry

def load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"version": 1, "created": now(), "checkpoints": {}, "selected": None, "test_runs": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"ERROR: registry {p} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)


def save(path: str, reg: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def live(reg: dict) -> dict:
    return {k: v for k, v in reg["checkpoints"].items() if v.get("status") != "deleted"}


# ---------------------------------------------------------------- scan

def find_checkpoints(root: Path) -> list[Path]:
    found: list[Path] = []
    if root.is_file():
        return [root] if root.suffix in FILE_EXT else []
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if HF_MARKERS & set(filenames) and any(Path(f).suffix in {".safetensors", ".bin"} for f in filenames):
            found.append(d)
            dirnames[:] = []
            continue
        for f in filenames:
            if Path(f).suffix in FILE_EXT and not f.startswith("optimizer") and f != "training_args.bin":
                found.append(d / f)
    return found


def cmd_scan(args) -> int:
    reg = load(args.registry)
    added = updated = 0
    for root in args.roots:
        r = Path(root)
        if not r.exists():
            print(f"WARN: {r} not found", file=sys.stderr)
            continue
        for ck in find_checkpoints(r):
            key = _rel(ck)
            m = STEP_RE.search(ck.name) or STEP_RE.search(ck.parent.name)
            e = EPOCH_RE.search(ck.name)
            entry = reg["checkpoints"].get(key, {"evals": {}, "pinned": None, "added": now()})
            qh = quick_hash(ck)
            if entry.get("quick_hash") and entry["quick_hash"] != qh:
                entry["evals"] = {}
                entry["note"] = f"content changed {now()}; old evals cleared"
                updated += 1
            elif "quick_hash" not in entry:
                added += 1
            entry.update(path=key, kind="dir" if ck.is_dir() else ck.suffix.lstrip("."),
                         size_bytes=size_of(ck), mtime=dt.datetime.fromtimestamp(ck.stat().st_mtime).isoformat(timespec="seconds"),
                         step=int(m.group(1)) if m else None, epoch=int(e.group(1)) if e else None,
                         quick_hash=qh, status="present")
            reg["checkpoints"][key] = entry
    save(args.registry, reg)
    print(f"Registry {args.registry}: {len(live(reg))} checkpoints ({added} new, {updated} changed)")
    return 0


# ---------------------------------------------------------------- eval / select / test

def _order(items):
    return sorted(items, key=lambda kv: (kv[1].get("step") is None, kv[1].get("step") or 0, kv[1].get("mtime", "")))


def run_eval(template: str, key: str, split: str, out_dir: Path) -> tuple[int, dict, str]:
    safe = re.sub(r"[^\w.-]+", "_", key)[-120:]
    out = out_dir / split / f"{safe}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = template.format(ckpt=key, out=out.as_posix(), split=split)
    log = out.with_suffix(".log")
    with log.open("w", encoding="utf-8") as fh:
        code = subprocess.call(cmd, shell=True, stdout=fh, stderr=subprocess.STDOUT)
    try:
        metrics = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metrics = {}
    return code, metrics, cmd


def cmd_eval(args) -> int:
    if args.split.lower() == "test":
        print("ERROR: use the `test` command for the test split (it runs once, on the selected checkpoint).",
              file=sys.stderr)
        return 2
    reg = load(args.registry)
    out_dir = Path(args.registry).parent / "evals"
    todo = [(k, v) for k, v in _order(live(reg).items())
            if not (args.only_new and args.split in v.get("evals", {}))]
    if not todo:
        print("Nothing to evaluate.")
        return 1
    failed = 0
    for key, entry in todo:
        code, metrics, cmd = run_eval(args.cmd, key, args.split, out_dir)
        ok = code == 0 and args.metric in metrics
        failed += not ok
        entry.setdefault("evals", {})[args.split] = {
            "metrics": metrics, "command": cmd, "exit_code": code, "time": now(),
            "quick_hash": entry.get("quick_hash"), "ok": ok}
        print(f"{'ok  ' if ok else 'FAIL'} {key}  {args.metric}={metrics.get(args.metric, 'n/a')}")
        save(args.registry, reg)
    return 0 if not failed else 1


def _metric(entry: dict, split: str, metric: str):
    ev = entry.get("evals", {}).get(split, {})
    if not ev.get("ok") or ev.get("quick_hash") != entry.get("quick_hash"):
        return None
    try:
        return float(ev["metrics"][metric])
    except (KeyError, TypeError, ValueError):
        return None


def ranked(reg: dict, split: str, metric: str, direction: str) -> list[tuple[str, float]]:
    scored = [(k, _metric(v, split, metric)) for k, v in live(reg).items()]
    scored = [(k, s) for k, s in scored if s is not None]
    return sorted(scored, key=lambda kv: kv[1], reverse=direction == "max")


def cmd_select(args) -> int:
    if args.split.lower() == "test":
        print("ERROR: never select on the test split (experiment-integrity).", file=sys.stderr)
        return 2
    reg = load(args.registry)
    order = ranked(reg, args.split, args.metric, args.direction)
    if not order:
        print(f"ERROR: no checkpoint has a valid '{args.metric}' on split '{args.split}'", file=sys.stderr)
        return 1
    best, value = order[0]
    reg["selected"] = {"path": best, "metric": args.metric, "split": args.split, "direction": args.direction,
                       "value": value, "time": now(), "quick_hash": reg["checkpoints"][best]["quick_hash"],
                       "candidates": len(order)}
    save(args.registry, reg)
    print(json.dumps(reg["selected"]))
    return 0


def cmd_test(args) -> int:
    reg = load(args.registry)
    sel = reg.get("selected")
    if not sel:
        print("ERROR: run `select` on a validation split first.", file=sys.stderr)
        return 1
    entry = reg["checkpoints"].get(sel["path"])
    if not entry or entry.get("status") == "deleted":
        print(f"ERROR: selected checkpoint {sel['path']} is missing", file=sys.stderr)
        return 1
    if args.force and not args.reason:
        print("ERROR: --force needs --reason (it is recorded in the registry and the report).", file=sys.stderr)
        return 2
    prior = [t for t in reg.get("test_runs", []) if t["path"] == sel["path"]]
    if prior and not args.force:
        print(f"ERROR: test split already evaluated for {sel['path']} at {prior[-1]['time']}. "
              "Re-running test after looking at it is test-set tuning; pass --force only with a written reason.",
              file=sys.stderr)
        return 1
    code, metrics, cmd = run_eval(args.cmd, sel["path"], "test", Path(args.registry).parent / "evals")
    run = {"path": sel["path"], "metrics": metrics, "command": cmd, "exit_code": code, "time": now(),
           "forced": bool(prior), "reason": args.reason or "", "run_index": len(prior) + 1}
    reg.setdefault("test_runs", []).append(run)
    save(args.registry, reg)
    print(json.dumps(run))
    return 0 if code == 0 and args.metric in metrics else 1


# ---------------------------------------------------------------- pin / prune / verify / report

def cmd_pin(args) -> int:
    reg = load(args.registry)
    key = _rel(Path(args.path))
    if key not in reg["checkpoints"]:
        print(f"ERROR: {key} not in registry (run scan first)", file=sys.stderr)
        return 1
    reg["checkpoints"][key]["pinned"] = args.reason
    save(args.registry, reg)
    print(f"Pinned {key}: {args.reason}")
    return 0


def plan_prune(reg: dict, keep_best: int, keep_last: int, keep_every: int, metric: str | None,
               split: str, direction: str) -> tuple[list[str], dict[str, str]]:
    items = _order(live(reg).items())
    keep: dict[str, str] = {}
    for k, _ in items[-keep_last:] if keep_last else []:
        keep[k] = "last"
    if metric:
        for k, _ in ranked(reg, split, metric, direction)[:keep_best]:
            keep.setdefault(k, f"best {metric}")
    for k, v in items:
        if v.get("pinned"):
            keep[k] = f"pinned: {v['pinned']}"
        if keep_every and v.get("step") and v["step"] % keep_every == 0:
            keep.setdefault(k, f"every {keep_every}")
    sel = (reg.get("selected") or {}).get("path")
    if sel:
        keep[sel] = "selected"
    for t in reg.get("test_runs", []):
        keep[t["path"]] = "has test result"
    delete = [k for k, _ in items if k not in keep]
    return delete, keep


def cmd_prune(args) -> int:
    reg = load(args.registry)
    if args.metric is None and args.keep_best:
        sel = reg.get("selected") or {}
        args.metric, args.split, args.direction = sel.get("metric"), sel.get("split", "val"), sel.get("direction", "max")
    if args.keep_best and not args.metric:
        print("ERROR: --keep-best needs --metric (or a prior `select`)", file=sys.stderr)
        return 2
    delete, keep = plan_prune(reg, args.keep_best, args.keep_last, args.keep_every, args.metric,
                              args.split, args.direction)
    freed = sum(reg["checkpoints"][k].get("size_bytes", 0) for k in delete)
    for k, why in keep.items():
        print(f"KEEP    {k}  ({why})")
    for k in delete:
        print(f"{'DELETE' if args.apply else 'WOULD DELETE'}  {k}")
    print(f"{len(delete)} checkpoint(s), {freed / 1e9:.2f} GB {'freed' if args.apply else 'would be freed'}")
    if not args.apply:
        print("Dry run. Re-run with --apply after the user confirms.")
        return 0
    for k in delete:
        p = Path(k)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        reg["checkpoints"][k].update(status="deleted", deleted=now())
    save(args.registry, reg)
    return 0


def cmd_verify(args) -> int:
    reg = load(args.registry)
    bad = 0
    for k, v in live(reg).items():
        p = Path(k)
        if not p.exists():
            print(f"MISSING  {k}")
            bad += 1
        elif quick_hash(p) != v.get("quick_hash"):
            print(f"CHANGED  {k}  (evals no longer match this content)")
            bad += 1
    print("OK" if not bad else f"{bad} problem(s)")
    return 0 if not bad else 1


def cmd_report(args) -> int:
    reg = load(args.registry)
    metrics = sorted({m for v in live(reg).values() for ev in v.get("evals", {}).values()
                      for m in (ev.get("metrics") or {}) if isinstance(ev.get("metrics", {}).get(m), (int, float))})
    splits = sorted({s for v in live(reg).values() for s in v.get("evals", {})})
    cols = [f"{s}:{m}" for s in splits for m in metrics]
    sel = (reg.get("selected") or {}).get("path")
    lines = ["# Checkpoints", "", f"Registry: `{args.registry}` · updated {now()}", "",
             "| Checkpoint | Step | Size (GB) | " + " | ".join(cols) + " | Notes |",
             "|---|---:|---:|" + "---:|" * len(cols) + "---|"]
    for k, v in _order(live(reg).items()):
        vals = []
        for c in cols:
            s, m = c.split(":", 1)
            x = v.get("evals", {}).get(s, {}).get("metrics", {}).get(m)
            vals.append(f"{x:.4g}" if isinstance(x, (int, float)) else "")
        notes = ", ".join(n for n in ["**selected**" if k == sel else "", f"pinned: {v['pinned']}" if v.get("pinned") else ""] if n)
        lines.append(f"| `{k}` | {v.get('step') if v.get('step') is not None else ''} | "
                     f"{v.get('size_bytes', 0) / 1e9:.2f} | " + " | ".join(vals) + f" | {notes} |")
    if reg.get("selected"):
        s = reg["selected"]
        lines += ["", f"Selected on **{s['split']}** `{s['metric']}` ({s['direction']}) = {s['value']:.4g} "
                      f"from {s['candidates']} candidates."]
    for t in reg.get("test_runs", []):
        lines.append(f"Test run {t['run_index']} on `{t['path']}` at {t['time']}: "
                     + ", ".join(f"{k}={v}" for k, v in (t.get("metrics") or {}).items())
                     + (f" (forced: {t['reason']})" if t.get("forced") else ""))
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", default="checkpoints/CHECKPOINT_REGISTRY.json")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("scan")
    p.add_argument("roots", nargs="+")
    p = sub.add_parser("eval")
    p.add_argument("--cmd", required=True, help="template with {ckpt}, {out}, {split}")
    p.add_argument("--metric", required=True)
    p.add_argument("--split", default="val")
    p.add_argument("--only-new", action="store_true")
    p = sub.add_parser("select")
    p.add_argument("--metric", required=True)
    p.add_argument("--direction", choices=["max", "min"], required=True)
    p.add_argument("--split", default="val")
    p = sub.add_parser("pin")
    p.add_argument("path")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("prune")
    p.add_argument("--keep-best", type=int, default=3)
    p.add_argument("--keep-last", type=int, default=2)
    p.add_argument("--keep-every", type=int, default=0)
    p.add_argument("--metric")
    p.add_argument("--split", default="val")
    p.add_argument("--direction", choices=["max", "min"], default="max")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("test")
    p.add_argument("--cmd", required=True)
    p.add_argument("--metric", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--reason")
    sub.add_parser("verify")
    p = sub.add_parser("report")
    p.add_argument("--out")
    args = ap.parse_args()
    return {"scan": cmd_scan, "eval": cmd_eval, "select": cmd_select, "pin": cmd_pin, "prune": cmd_prune,
            "test": cmd_test, "verify": cmd_verify, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
