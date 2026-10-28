#!/usr/bin/env python3
"""Deterministic guard for /paper-humanize: prose may change, facts may not.

Commands:
  snapshot FILES... --dir D      copy originals into D (with a sha256 manifest)
  check --dir D [--json-out F]   compare every snapshotted file with its current version
  restore --dir D [FILES...]     put originals back (all, or the named files)

Protected (any change -> FAIL): numbers in prose and tables, math, citation keys,
\\ref / \\label keys, environment order, \\input / \\includegraphics targets, URLs,
LaTeX comments, verbatim / code blocks; for Markdown: numbers, code, links.
Warnings (never fail): dashes used as punctuation, residual AI words, sentences
over 35 words, length change outside 60-115% of the original.

Exit codes: 0 PASS (warnings allowed), 1 FAIL, 2 bad arguments / missing files.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

MATH_ENVS = r"equation|align|alignat|gather|multline|eqnarray|math|displaymath|flalign"
CODE_ENVS = r"verbatim|lstlisting|minted|comment"
AI_WORDS = [
    "delve", "delves", "delving", "pivotal", "tapestry", "testament", "underscore", "underscores",
    "showcase", "showcases", "showcasing", "intricate", "intricacies", "realm", "seamless",
    "seamlessly", "meticulous", "meticulously", "paramount", "groundbreaking", "vibrant",
    "paves the way", "sheds light", "it is worth noting", "in the realm of",
]
REF_CMDS = r"(?:ref|eqref|cref|Cref|autoref|pageref|nameref|label|vref)"
CITE_CMDS = r"(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|parencite|textcite|autocite|nocite)\*?"
NUM_RE = re.compile(r"(?<![\w.])[-+−]?\d+(?:[.,]\d+)*%?")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- extraction

def split_latex(text: str) -> tuple[str, dict]:
    """Return (prose, protected) for a LaTeX source."""
    prot: dict[str, list] = {"comments": [], "code": [], "math": []}

    def take(kind):
        def repl(m):
            prot[kind].append(re.sub(r"\s+", " ", m.group(0)).strip())
            return f" \x00{kind[0].upper()}{len(prot[kind])}\x00 "
        return repl

    def comment(m):
        prot["comments"].append(m.group(1).rstrip())
        return ""

    text = re.sub(rf"\\begin\{{({CODE_ENVS})\*?\}}.*?\\end\{{\1\*?\}}", take("code"), text, flags=re.S)
    text = re.sub(r"(?<!\\)%(.*)", comment, text)
    text = re.sub(rf"\\begin\{{({MATH_ENVS})(\*?)\}}.*?\\end\{{\1\2\}}", take("math"), text, flags=re.S)
    for pat in (r"\$\$.*?\$\$", r"\\\[.*?\\\]", r"\\\(.*?\\\)", r"(?<!\\)\$(?:\\\$|[^$])+?(?<!\\)\$"):
        text = re.sub(pat, take("math"), text, flags=re.S)
    prot["cites"] = sorted(k.strip() for grp in re.findall(rf"\\{CITE_CMDS}(?:\[[^\]]*\]){{0,2}}\{{([^}}]*)\}}", text)
                           for k in grp.split(",") if k.strip())
    prot["refs"] = sorted(re.findall(rf"\\{REF_CMDS}\{{([^}}]*)\}}", text))
    prot["envs"] = re.findall(r"\\(begin|end)\{([^}]*)\}", text)
    prot["files"] = re.findall(r"\\(?:input|include|includegraphics|bibliography|subfile)(?:\[[^\]]*\])?\{([^}]*)\}", text)
    prot["urls"] = re.findall(r"\\(?:url|href)\{([^}]*)\}", text)
    prose = re.sub(rf"\\{CITE_CMDS}(?:\[[^\]]*\]){{0,2}}\{{[^}}]*\}}", " ", text)
    prose = re.sub(rf"\\{REF_CMDS}\{{[^}}]*\}}", " ", prose)
    prose = re.sub(r"\\(?:input|include|includegraphics|bibliography|subfile|url|href|begin|end)(?:\[[^\]]*\])?\{[^}]*\}", " ", prose)
    prot["numbers"] = sorted(NUM_RE.findall(re.sub(r"\x00[A-Z]\d+\x00", " ", prose)))
    return prose, prot


def split_markdown(text: str) -> tuple[str, dict]:
    prot: dict[str, list] = {"code": [], "math": []}

    def take(kind):
        def repl(m):
            prot[kind].append(m.group(0))
            return " "
        return repl

    text = re.sub(r"```.*?```", take("code"), text, flags=re.S)
    text = re.sub(r"`[^`\n]+`", take("code"), text)
    text = re.sub(r"\$\$.*?\$\$|(?<!\\)\$[^$\n]+?\$", take("math"), text, flags=re.S)
    prot["urls"] = sorted(re.findall(r"\]\(([^)]+)\)|<(https?://[^>]+)>", text))
    prot["cites"] = sorted(re.findall(r"\[@([^\]]+)\]", text))
    prose = re.sub(r"\]\([^)]+\)", "]", text)
    prot["numbers"] = sorted(NUM_RE.findall(prose))
    return prose, prot


def split_any(path: Path, text: str):
    return split_latex(text) if path.suffix.lower() in {".tex", ".ltx", ".sty"} else split_markdown(text)


# ---------------------------------------------------------------- warnings

def _plain(prose: str) -> str:
    prose = re.sub(r"\x00[A-Z]\d+\x00", " X ", prose)
    prose = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", prose)
    return re.sub(r"[{}]", " ", prose)


def warnings_for(orig_prose: str, new_prose: str) -> list[str]:
    out = []
    plain = _plain(new_prose)
    em = re.findall(r"---|—", new_prose)
    rest = re.sub(r"---|—", " ", new_prose)
    dashes = len(em) + len(re.findall(r"(?<!\d)\s--\s(?!\d)|(?<!\d)–(?!\d)", rest))
    if dashes:
        out.append(f"dash_as_punctuation: {dashes} (remove unless the voice sample uses dashes; keep number ranges like 4--7)")
    low = plain.lower()
    residue = {w: low.count(w) for w in AI_WORDS if re.search(rf"\b{re.escape(w)}\b", low)}
    if residue:
        out.append("ai_words_left: " + ", ".join(f"{w} x{n}" for w, n in residue.items()))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", plain)) if s.strip()]
    long = [s for s in sentences if len(s.split()) > 35]
    if long:
        out.append(f"long_sentences_over_35_words: {len(long)}")
    o, n = len(_plain(orig_prose).split()), len(plain.split())
    if o and not 0.6 <= n / o <= 1.15:
        out.append(f"length_ratio: {n / o:.2f} (words {o} -> {n}; check for lost or padded content)")
    return out


# ---------------------------------------------------------------- comparison

def compare(path: Path, orig: str, new: str) -> dict:
    op, oprot = split_any(path, orig)
    np_, nprot = split_any(path, new)
    failures = []
    for key in sorted(set(oprot) | set(nprot)):
        a, b = oprot.get(key, []), nprot.get(key, [])
        if a == b:
            continue
        if key in {"numbers", "cites", "refs", "urls", "files", "comments", "math", "code"}:
            ca, cb = Counter(map(str, a)), Counter(map(str, b))
            lost, added = list((ca - cb).elements()), list((cb - ca).elements())
            if not lost and not added and key in {"math", "code", "comments"}:
                failures.append({"check": key, "detail": "order changed"})
            elif lost or added:
                failures.append({"check": key, "lost": lost[:20], "added": added[:20]})
        else:
            failures.append({"check": key, "detail": "sequence changed"})
    return {"status": "FAIL" if failures else "PASS", "failures": failures,
            "warnings": warnings_for(op, np_)}


# ---------------------------------------------------------------- commands

def _manifest(d: Path) -> dict:
    try:
        return json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"ERROR: no manifest.json in {d}; run snapshot first.", file=sys.stderr)
        sys.exit(2)


def cmd_snapshot(args) -> int:
    d = Path(args.dir)
    files = []
    for f in args.files:
        p = Path(f)
        if not p.is_file():
            print(f"ERROR: {p} not found", file=sys.stderr)
            return 2
        rel = Path(p.as_posix().lstrip("/").replace(":", ""))
        dst = d / "orig" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        files.append({"path": p.as_posix(), "copy": (Path("orig") / rel).as_posix(), "sha256": _sha(p)})
    (d / "manifest.json").write_text(json.dumps({"files": files}, indent=2), encoding="utf-8")
    print(f"Snapshot: {len(files)} file(s) -> {d}")
    return 0


def cmd_check(args) -> int:
    d = Path(args.dir)
    rows, overall = [], "PASS"
    for f in _manifest(d)["files"]:
        cur = Path(f["path"])
        if not cur.is_file():
            rows.append({"path": f["path"], "status": "FAIL", "failures": [{"check": "file", "detail": "missing"}], "warnings": []})
            overall = "FAIL"
            continue
        orig = (d / f["copy"]).read_text(encoding="utf-8", errors="replace")
        new = cur.read_text(encoding="utf-8", errors="replace")
        row = {"path": f["path"], "changed": _sha(cur) != f["sha256"], **compare(cur, orig, new)}
        overall = "FAIL" if row["status"] == "FAIL" else overall
        rows.append(row)
    report = {"verdict": overall, "files": rows}
    out = json.dumps(report, indent=2, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(out, encoding="utf-8")
    for r in rows:
        print(f"{r['status']:4}  {r['path']}" + ("" if r.get("changed", True) else "  (unchanged)"))
        for fl in r["failures"]:
            print(f"      FAIL {fl['check']}: " + json.dumps({k: v for k, v in fl.items() if k != 'check'}, ensure_ascii=True))
        for w in r["warnings"]:
            print(f"      warn {w}".encode("ascii", "replace").decode())
    print(f"VERDICT: {overall}")
    return 0 if overall == "PASS" else 1


def cmd_restore(args) -> int:
    d = Path(args.dir)
    want = {Path(f).as_posix() for f in args.files}
    n = 0
    for f in _manifest(d)["files"]:
        if want and f["path"] not in want:
            continue
        shutil.copy2(d / f["copy"], f["path"])
        n += 1
    print(f"Restored {n} file(s) from {d}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("files", nargs="+")
    s.add_argument("--dir", required=True)
    c = sub.add_parser("check")
    c.add_argument("--dir", required=True)
    c.add_argument("--json-out")
    r = sub.add_parser("restore")
    r.add_argument("files", nargs="*")
    r.add_argument("--dir", required=True)
    args = ap.parse_args()
    return {"snapshot": cmd_snapshot, "check": cmd_check, "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
