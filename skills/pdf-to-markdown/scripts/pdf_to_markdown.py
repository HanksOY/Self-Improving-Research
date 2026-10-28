#!/usr/bin/env python3
"""Batch-convert PDFs to Markdown with OpenDataLoader PDF and verify every output.

Requires Java 11+, Python 3.10+, and `pip install -U opendataloader-pdf`
(hybrid mode: `pip install -U "opendataloader-pdf[hybrid]"` plus a running
`opendataloader-pdf-hybrid --port 5002` server).

Commands
--------
check     Report Java / Python / opendataloader-pdf status. Exit 0 = ready, 2 = missing.
convert   Convert PDF files and/or folders in ONE batch call, then verify outputs and
          write conversion-status.{csv,json} into the output folder.

Examples
--------
python pdf_to_markdown.py check
python pdf_to_markdown.py convert papers/ --out papers/markdown
python pdf_to_markdown.py convert a.pdf b.pdf --out md/ --mode hybrid
python pdf_to_markdown.py convert library/ --recursive --out md/ --images external
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_FIELDS = ["pdf_path", "markdown_path", "status", "chars", "failure_reason", "mode"]


def _java_version() -> tuple[int | None, str]:
    if not shutil.which("java"):
        return None, "java not found on PATH"
    proc = subprocess.run(["java", "-version"], capture_output=True, text=True)
    line = (proc.stderr or proc.stdout).splitlines()[0] if (proc.stderr or proc.stdout) else ""
    m = re.search(r'version "(\d+)(?:\.(\d+))?', line)
    if not m:
        return None, line or "unparseable java -version output"
    major = int(m.group(1))
    if major == 1 and m.group(2):
        major = int(m.group(2))
    return major, line


def _odl_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("opendataloader-pdf")
    except PackageNotFoundError:
        return None


def _hybrid_available() -> bool:
    try:
        import docling  # noqa: F401
        return True
    except ImportError:
        return False


def check() -> dict[str, Any]:
    java_major, java_line = _java_version()
    odl = _odl_version()
    py_ok = sys.version_info >= (3, 10)
    report = {
        "python": sys.version.split()[0],
        "python_ok": py_ok,
        "java": java_line,
        "java_ok": java_major is not None and java_major >= 11,
        "opendataloader_pdf": odl,
        "opendataloader_ok": odl is not None,
        "hybrid_extra_installed": _hybrid_available(),
    }
    report["ready"] = report["python_ok"] and report["java_ok"] and report["opendataloader_ok"]
    fixes = []
    if not report["java_ok"]:
        fixes.append("Install JDK 11+ (https://adoptium.net/) and put java on PATH.")
    if not report["python_ok"]:
        fixes.append("Use Python 3.10+.")
    if not report["opendataloader_ok"]:
        fixes.append('pip install -U opendataloader-pdf   (hybrid: pip install -U "opendataloader-pdf[hybrid]")')
    report["fixes"] = fixes
    return report


def _collect_pdfs(inputs: list[str], recursive: bool) -> tuple[list[Path], list[str]]:
    pdfs: list[Path] = []
    missing: list[str] = []
    seen: set[Path] = set()
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            found = p.rglob("*") if recursive else p.glob("*")
            candidates = sorted(f for f in found if f.is_file() and f.suffix.lower() == ".pdf")
        elif p.is_file() and p.suffix.lower() == ".pdf":
            candidates = [p]
        else:
            missing.append(raw)
            continue
        for f in candidates:
            r = f.resolve()
            if r not in seen:
                seen.add(r)
                pdfs.append(r)
    return pdfs, missing


def _stem_collisions(pdfs: list[Path]) -> dict[str, list[str]]:
    by_stem: dict[str, list[str]] = {}
    for p in pdfs:
        by_stem.setdefault(p.stem.lower(), []).append(str(p))
    return {k: v for k, v in by_stem.items() if len(v) > 1}


def _convert_kwargs(args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "output_dir": str(out_dir),
        "format": "markdown",
        "quiet": True,
        "image_output": args.images,
    }
    if args.images == "external":
        kwargs["image_dir"] = str(out_dir / "images")
    if args.page_separator:
        kwargs["markdown_page_separator"] = "\n\n<!-- page %page-number% -->\n\n"
    if args.mode == "hybrid":
        kwargs["hybrid"] = "docling-fast"
        kwargs["hybrid_fallback"] = True
        if args.hybrid_full:
            kwargs["hybrid_mode"] = "full"
        if args.hybrid_url:
            kwargs["hybrid_url"] = args.hybrid_url
    return kwargs


def _write_status(out_dir: Path, rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    with open(out_dir / "conversion-status.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=STATUS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with open(out_dir / "conversion-status.json", "w", encoding="utf-8") as fh:
        json.dump({**meta, "papers": rows}, fh, ensure_ascii=False, indent=2)


def convert(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    env = check()
    if not env["ready"]:
        return {"error": "environment not ready", "check": env}, 2
    if args.mode == "hybrid" and not env["hybrid_extra_installed"]:
        return {"error": 'hybrid mode needs: pip install -U "opendataloader-pdf[hybrid]"', "check": env}, 2

    pdfs, missing = _collect_pdfs(args.inputs, args.recursive)
    if not pdfs:
        return {"error": "no PDF files found", "missing_inputs": missing}, 1
    collisions = _stem_collisions(pdfs)
    if collisions:
        return {
            "error": "several PDFs share a file name; outputs would overwrite each other. "
                     "Convert these in separate runs with different --out folders.",
            "collisions": collisions,
        }, 1

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    import opendataloader_pdf

    kwargs = _convert_kwargs(args, out_dir)
    errors: dict[Path, str] = {}
    batch_error = None
    try:
        opendataloader_pdf.convert(input_path=[str(p) for p in pdfs], **kwargs)
    except Exception as exc:  # one bad PDF can abort the whole JVM batch; isolate it
        batch_error = f"{type(exc).__name__}: {exc}"
        for p in pdfs:
            if (out_dir / f"{p.stem}.md").is_file():
                continue
            try:
                opendataloader_pdf.convert(input_path=str(p), **kwargs)
            except Exception as one_exc:
                errors[p] = f"{type(one_exc).__name__}: {one_exc}"[:300]

    rows: list[dict[str, Any]] = []
    for p in pdfs:
        md = out_dir / f"{p.stem}.md"
        row = {"pdf_path": str(p), "markdown_path": "", "status": "", "chars": 0,
               "failure_reason": "", "mode": args.mode}
        if md.is_file():
            chars = len(md.read_text(encoding="utf-8", errors="replace").strip())
            row["markdown_path"] = str(md)
            row["chars"] = chars
            if chars < args.min_chars:
                row["status"] = "needs-manual-review"
                row["failure_reason"] = f"output has {chars} chars (< {args.min_chars}); scanned or image-only PDF?"
            else:
                row["status"] = "converted"
        else:
            row["status"] = "conversion-failed"
            row["failure_reason"] = errors.get(p, batch_error or "no .md output produced")
        rows.append(row)
    for raw in missing:
        rows.append({"pdf_path": raw, "markdown_path": "", "status": "pdf-not-found", "chars": 0,
                     "failure_reason": "path does not exist or is not a .pdf", "mode": args.mode})

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    meta = {
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "output_dir": str(out_dir),
        "mode": args.mode,
        "opendataloader_pdf": env["opendataloader_pdf"],
        "java": env["java"],
        "batch_error": batch_error,
        "counts": counts,
    }
    _write_status(out_dir, rows, meta)
    exit_code = 0 if counts.get("converted", 0) == len(rows) else 3
    return {**meta, "status_csv": str(out_dir / "conversion-status.csv"), "papers": rows}, exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch PDF -> Markdown via OpenDataLoader PDF, with per-file verification.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Check Java / Python / opendataloader-pdf")

    c = sub.add_parser("convert", help="Convert PDFs (files and/or folders) in one batch")
    c.add_argument("inputs", nargs="+", help="PDF files and/or folders")
    c.add_argument("--out", required=True, help="Output folder for .md files and status files")
    c.add_argument("--recursive", action="store_true", help="Search folders recursively")
    c.add_argument("--mode", default="standard", choices=("standard", "hybrid"),
                   help="hybrid = docling-fast backend (needs running server); default standard")
    c.add_argument("--hybrid-full", action="store_true",
                   help="Send every page to the hybrid backend (scanned / image PDFs)")
    c.add_argument("--hybrid-url", default=None, help="Hybrid server URL (default http://localhost:5002)")
    c.add_argument("--images", default="off", choices=("off", "external", "embedded"),
                   help="Image extraction (default off = smallest Markdown)")
    c.add_argument("--page-separator", action="store_true",
                   help="Insert <!-- page N --> markers between pages")
    c.add_argument("--min-chars", type=int, default=500,
                   help="Outputs shorter than this are flagged needs-manual-review (default 500)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "check":
        report = check()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 2
    result, code = convert(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
