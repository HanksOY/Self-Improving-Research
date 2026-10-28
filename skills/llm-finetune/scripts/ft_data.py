#!/usr/bin/env python3
"""Dataset checks and conversion for /llm-finetune (JSONL; standard library only).

Formats:
  chat        {"messages": [{"role": "system|user|assistant|tool", "content": "..."}, ...]}
  completion  {"prompt": "...", "completion": "..."}
  preference  {"prompt": "..." | [messages], "chosen": "..." | [messages], "rejected": ...}
  alpaca      {"instruction": "...", "input": "...", "output": "..."}      (convert only)
  sharegpt    {"conversations": [{"from": "human|gpt|system", "value": "..."}]}  (convert only)

Commands:
  inspect FILE                         detect format, count rows, show length stats
  validate FILE --format F [--max-tokens N] [--report R.json]
  convert FILE --from alpaca|sharegpt|completion --to chat --out OUT
  split FILE --val 0.05 [--seed 0] --out-dir D     dedupe, then split with no duplicate across splits
  leakage TRAIN EVAL [--n 13]          n-gram overlap of EVAL rows with TRAIN (contamination check)

Token counts are estimates (characters / 4) unless --tokenizer names a local
Hugging Face tokenizer (needs `transformers`).
Exit codes: 0 ok, 1 problems found, 2 bad arguments / unreadable file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
from pathlib import Path

ROLES = {"system", "user", "assistant", "tool"}


def read_jsonl(path: str) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    try:
        fh = open(path, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    with fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                errors.append(f"line {i}: invalid JSON ({exc.msg})")
                continue
            if not isinstance(obj, dict):
                errors.append(f"line {i}: row is not a JSON object")
                continue
            obj["_line"] = i
            rows.append(obj)
    return rows, errors


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps({k: v for k, v in r.items() if k != "_line"}, ensure_ascii=False) + "\n")


def detect(row: dict) -> str:
    if "messages" in row:
        return "chat"
    if {"chosen", "rejected"} <= row.keys():
        return "preference"
    if {"prompt", "completion"} <= row.keys():
        return "completion"
    if "instruction" in row and "output" in row:
        return "alpaca"
    if "conversations" in row:
        return "sharegpt"
    if "text" in row:
        return "text"
    return "unknown"


def text_of(row: dict) -> str:
    def flat(v):
        if isinstance(v, list):
            return "\n".join(str(m.get("content", "")) if isinstance(m, dict) else str(m) for m in v)
        return "" if v is None else str(v)
    return "\n".join(flat(row.get(k)) for k in
                     ("messages", "prompt", "completion", "chosen", "rejected", "instruction", "input",
                      "output", "text") if k in row)


def row_hash(row: dict) -> str:
    norm = re.sub(r"\s+", " ", text_of(row)).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def make_counter(tokenizer: str | None):
    if not tokenizer:
        return lambda s: max(1, len(s) // 4), "estimate (chars/4)"
    try:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(tokenizer)
        return lambda s: len(tok(s, add_special_tokens=False)["input_ids"]), f"tokenizer {tokenizer}"
    except Exception as exc:  # fall back, but say so
        print(f"WARN: tokenizer unavailable ({exc}); using chars/4 estimate", file=sys.stderr)
        return lambda s: max(1, len(s) // 4), "estimate (chars/4)"


# ---------------------------------------------------------------- validation

def _check_messages(msgs, where: str, need_last_assistant: bool) -> list[str]:
    errs = []
    if not isinstance(msgs, list) or not msgs:
        return [f"{where}: messages must be a non-empty list"]
    prev = None
    for j, m in enumerate(msgs):
        if not isinstance(m, dict):
            errs.append(f"{where}: message {j} is not an object")
            continue
        role, content = m.get("role"), m.get("content")
        if role not in ROLES:
            errs.append(f"{where}: message {j} has role {role!r} (allowed {sorted(ROLES)})")
        if role == "system" and j != 0:
            errs.append(f"{where}: system message at position {j} (must be first)")
        if not (isinstance(content, str) and content.strip()) and not m.get("tool_calls"):
            errs.append(f"{where}: message {j} ({role}) has empty content")
        if role == prev and role in {"user", "assistant"}:
            errs.append(f"{where}: two {role} messages in a row at {j}")
        prev = role
    if need_last_assistant and msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") != "assistant":
        errs.append(f"{where}: last message is not from the assistant (nothing to learn)")
    if not any(isinstance(m, dict) and m.get("role") == "assistant" for m in msgs):
        errs.append(f"{where}: no assistant message")
    return errs


def validate_rows(rows: list[dict], fmt: str, max_tokens: int, count) -> dict:
    problems: list[str] = []
    seen: dict[str, int] = {}
    dups, lengths, too_long = 0, [], 0
    for r in rows:
        where = f"line {r['_line']}"
        kind = detect(r)
        if kind != fmt:
            problems.append(f"{where}: looks like '{kind}', expected '{fmt}'")
            continue
        if fmt == "chat":
            problems += _check_messages(r["messages"], where, need_last_assistant=True)
        elif fmt == "completion":
            for k in ("prompt", "completion"):
                if not (isinstance(r.get(k), str) and r[k].strip()):
                    problems.append(f"{where}: empty or non-string '{k}'")
        elif fmt == "preference":
            if r.get("chosen") == r.get("rejected"):
                problems.append(f"{where}: chosen and rejected are identical")
            for k in ("prompt", "chosen", "rejected"):
                v = r.get(k)
                if isinstance(v, list):
                    problems += _check_messages(v, f"{where} {k}", need_last_assistant=(k != "prompt"))
                elif not (isinstance(v, str) and v.strip()):
                    problems.append(f"{where}: empty '{k}'")
        h = row_hash(r)
        if h in seen:
            dups += 1
        seen.setdefault(h, r["_line"])
        n = count(text_of(r))
        lengths.append(n)
        too_long += n > max_tokens
    stats = {}
    if lengths:
        q = sorted(lengths)
        stats = {"min": q[0], "median": int(statistics.median(q)), "p95": q[int(0.95 * (len(q) - 1))],
                 "max": q[-1], "total": sum(q)}
    return {"rows": len(rows), "format": fmt, "problems": problems, "duplicates": dups,
            "over_max_tokens": too_long, "max_tokens": max_tokens, "token_stats": stats}


# ---------------------------------------------------------------- commands

def cmd_inspect(args) -> int:
    rows, errors = read_jsonl(args.file)
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[detect(r)] = kinds.get(detect(r), 0) + 1
    count, how = make_counter(args.tokenizer)
    lengths = sorted(count(text_of(r)) for r in rows)
    out = {"file": args.file, "rows": len(rows), "json_errors": len(errors), "formats": kinds,
           "tokens": how}
    if lengths:
        out["token_stats"] = {"median": int(statistics.median(lengths)),
                              "p95": lengths[int(0.95 * (len(lengths) - 1))], "max": lengths[-1],
                              "total": sum(lengths)}
    print(json.dumps(out, indent=2))
    return 0 if not errors and len(kinds) == 1 else 1


def cmd_validate(args) -> int:
    rows, errors = read_jsonl(args.file)
    count, how = make_counter(args.tokenizer)
    report = validate_rows(rows, args.format, args.max_tokens, count)
    report["json_errors"] = errors
    report["tokens"] = how
    ok = not errors and not report["problems"] and report["duplicates"] == 0 and report["over_max_tokens"] == 0
    report["verdict"] = "PASS" if ok else "FAIL"
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    shown = {k: v for k, v in report.items() if k not in {"problems", "json_errors"}}
    shown["problems"] = (errors + report["problems"])[:25]
    shown["problem_count"] = len(errors) + len(report["problems"])
    print(json.dumps(shown, indent=2, ensure_ascii=False))
    return 0 if ok else 1


def convert_row(r: dict, src: str) -> dict:
    if src == "alpaca":
        user = r["instruction"] + (("\n\n" + r["input"]) if r.get("input") else "")
        msgs = ([{"role": "system", "content": r["system"]}] if r.get("system") else []) + \
               [{"role": "user", "content": user}, {"role": "assistant", "content": r["output"]}]
    elif src == "sharegpt":
        role = {"human": "user", "user": "user", "gpt": "assistant", "assistant": "assistant",
                "system": "system", "tool": "tool", "function_call": "assistant", "observation": "tool"}
        msgs = [{"role": role.get(m.get("from"), m.get("from")), "content": m.get("value", "")}
                for m in r["conversations"]]
    elif src == "completion":
        msgs = [{"role": "user", "content": r["prompt"]}, {"role": "assistant", "content": r["completion"]}]
    else:
        raise ValueError(src)
    return {"messages": msgs}


def cmd_convert(args) -> int:
    if args.to != "chat":
        print("ERROR: only --to chat is supported", file=sys.stderr)
        return 2
    rows, errors = read_jsonl(args.file)
    out, bad = [], 0
    for r in rows:
        try:
            out.append(convert_row(r, args.src))
        except (KeyError, TypeError, AttributeError):
            bad += 1
            print(f"WARN: line {r['_line']} skipped (missing fields for {args.src})", file=sys.stderr)
    write_jsonl(Path(args.out), out)
    print(f"Converted {len(out)} rows to {args.out}; skipped {bad}; JSON errors {len(errors)}")
    return 0 if not bad and not errors else 1


def cmd_split(args) -> int:
    rows, errors = read_jsonl(args.file)
    uniq: dict[str, dict] = {}
    for r in rows:
        uniq.setdefault(row_hash(r), r)
    items = list(uniq.values())
    random.Random(args.seed).shuffle(items)
    n_val = max(1, round(len(items) * args.val)) if items else 0
    val, train = items[:n_val], items[n_val:]
    d = Path(args.out_dir)
    write_jsonl(d / "train.jsonl", train)
    write_jsonl(d / "val.jsonl", val)
    meta = {"source": args.file, "rows_in": len(rows), "duplicates_removed": len(rows) - len(items),
            "train": len(train), "val": len(val), "seed": args.seed, "val_fraction": args.val,
            "source_sha256": hashlib.sha256(Path(args.file).read_bytes()).hexdigest()}
    (d / "split_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta))
    return 0 if not errors else 1


def _shingles(text: str, n: int) -> set[int]:
    words = re.findall(r"\w+", text.lower())
    return {hash(" ".join(words[i:i + n])) for i in range(max(0, len(words) - n + 1))}


def cmd_leakage(args) -> int:
    train, _ = read_jsonl(args.train)
    evalr, _ = read_jsonl(args.eval)
    bank: set[int] = set()
    exact = {row_hash(r) for r in train}
    for r in train:
        bank |= _shingles(text_of(r), args.n)
    hits, exact_hits = [], 0
    for r in evalr:
        if row_hash(r) in exact:
            exact_hits += 1
        sh = _shingles(text_of(r), args.n)
        if sh and len(sh & bank) / len(sh) >= args.threshold:
            hits.append(r["_line"])
    out = {"train_rows": len(train), "eval_rows": len(evalr), "ngram": args.n, "threshold": args.threshold,
           "exact_duplicates": exact_hits, "contaminated_rows": len(hits),
           "contaminated_fraction": round(len(hits) / len(evalr), 4) if evalr else 0.0,
           "eval_lines": hits[:50]}
    print(json.dumps(out, indent=2))
    return 0 if not hits and not exact_hits else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect")
    p.add_argument("file")
    p.add_argument("--tokenizer")
    p = sub.add_parser("validate")
    p.add_argument("file")
    p.add_argument("--format", required=True, choices=["chat", "completion", "preference"])
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--tokenizer")
    p.add_argument("--report")
    p = sub.add_parser("convert")
    p.add_argument("file")
    p.add_argument("--from", dest="src", required=True, choices=["alpaca", "sharegpt", "completion"])
    p.add_argument("--to", default="chat")
    p.add_argument("--out", required=True)
    p = sub.add_parser("split")
    p.add_argument("file")
    p.add_argument("--val", type=float, default=0.05)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out-dir", required=True)
    p = sub.add_parser("leakage")
    p.add_argument("train")
    p.add_argument("eval")
    p.add_argument("--n", type=int, default=13)
    p.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()
    return {"inspect": cmd_inspect, "validate": cmd_validate, "convert": cmd_convert, "split": cmd_split,
            "leakage": cmd_leakage}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
