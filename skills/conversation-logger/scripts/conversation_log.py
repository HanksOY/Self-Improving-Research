#!/usr/bin/env python3
"""Conversation logger for ARIS sessions (Claude Code and Codex transcripts).

User messages are copied verbatim from the host transcript by this script; the
agent only supplies one compressed line per round. Standard library only.

Commands:
  hook                 Claude Code Stop / SessionEnd hook (reads hook JSON on stdin)
  status               show logged / pending rounds for a session
  pending              write the unlogged rounds as JSON for the agent to compress
  append               append compressed rounds (entries JSON) to the log
  flush                append all unlogged rounds with automatic one-line summaries

Exit codes: 0 ok, 1 bad input / nothing to do, 2 transcript not found.
The hook command always exits 0 so it can never break a session.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

EVERY = int(os.environ.get("ARIS_LOG_EVERY", "20") or 20)
SCRIPT = Path(__file__).resolve()

# Claude Code built-in slash commands are UI actions, not conversation rounds.
BUILTIN_COMMANDS = {
    "model", "clear", "compact", "config", "cost", "help", "status", "login", "logout",
    "mcp", "memory", "permissions", "resume", "theme", "vim", "fast", "effort", "doctor",
    "init", "agents", "hooks", "ide", "export", "context", "usage", "rewind", "exit",
    "add-dir", "bug", "release-notes", "terminal-setup", "upgrade", "plugin", "artifacts",
}
INJECTED_RE = re.compile(
    r"<(system-reminder|ide_selection|ide_opened_file|ide_diagnostics)>.*?</\1>\s*", re.S
)
SKIP_PREFIXES = ("<task-notification>", "<local-command-caveat>", "<local-command-stdout>",
                 "<local-command-stderr>", "<environment_context>", "<user_instructions>")
FILE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


# ---------------------------------------------------------------- transcript parsing

def _ts(value: str | None) -> str:
    if not value:
        return ""
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return t.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value[:16]


def _clean_user_text(text: str) -> str:
    return INJECTED_RE.sub("", text).strip()


def _command_prompt(text: str) -> str | None:
    """Turn a <command-name> record into '/name args'; None for built-in commands."""
    m = re.search(r"<command-name>/?([^<]+)</command-name>", text)
    if not m:
        return text
    name = m.group(1).strip()
    if name.split(":")[-1] in BUILTIN_COMMANDS:
        return None
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    return f"/{name} {args.group(1).strip()}".rstrip() if args else f"/{name}"


def _new_round(user: str, ts: str) -> dict:
    return {"user": user, "time": _ts(ts), "texts": [], "tools": Counter(), "files": []}


def parse_claude(path: Path) -> tuple[str, str, list[dict]]:
    session, cwd, rounds = path.stem, "", []
    seen_text: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"type":"user"' not in line and '"type":"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("isSidechain"):
                continue
            cwd = cwd or rec.get("cwd", "")
            session = rec.get("sessionId", session)
            msg = rec.get("message") or {}
            content = msg.get("content")
            if rec.get("type") == "user":
                if rec.get("isMeta") or rec.get("isCompactSummary"):
                    continue
                if isinstance(content, list):
                    if any(b.get("type") == "tool_result" for b in content):
                        continue
                    parts = []
                    for b in content:
                        if b.get("type") == "text":
                            parts.append(b.get("text", ""))
                        elif b.get("type") == "image":
                            parts.append("[image]")
                    text = "\n".join(parts)
                else:
                    text = content or ""
                text = _clean_user_text(text)
                if not text or text.startswith(SKIP_PREFIXES):
                    continue
                if "<command-name>" in text:
                    text = _command_prompt(text)
                    if text is None:
                        continue
                elif "permissionMode" not in rec:
                    continue
                rounds.append(_new_round(text, rec.get("timestamp")))
            elif rounds and isinstance(content, list):
                cur = rounds[-1]
                for b in content:
                    if b.get("type") == "text" and b.get("text", "").strip():
                        key = (msg.get("id", ""), b["text"])
                        if key not in seen_text:
                            seen_text.add(key)
                            cur["texts"].append(b["text"].strip())
                    elif b.get("type") == "tool_use":
                        name = b.get("name", "?")
                        cur["tools"][name] += 1
                        fp = (b.get("input") or {}).get("file_path")
                        if name in FILE_TOOLS and fp and fp not in cur["files"]:
                            cur["files"].append(fp)
    return session, cwd, rounds


def parse_codex(path: Path) -> tuple[str, str, list[dict]]:
    session, cwd, rounds = path.stem, "", []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = rec.get("payload") or {}
            if rec.get("type") == "session_meta":
                session, cwd = p.get("id", session), p.get("cwd", cwd)
            elif rec.get("type") == "event_msg" and p.get("type") == "user_message":
                text = _clean_user_text(p.get("message") or "")
                if text and not text.startswith(SKIP_PREFIXES):
                    rounds.append(_new_round(text, rec.get("timestamp")))
            elif rounds and rec.get("type") == "event_msg" and p.get("type") == "agent_message":
                if (p.get("message") or "").strip():
                    rounds[-1]["texts"].append(p["message"].strip())
            elif rounds and rec.get("type") == "response_item" and p.get("type") == "function_call":
                rounds[-1]["tools"][p.get("name", "?")] += 1
    return session, cwd, rounds


def parse(path: Path) -> tuple[str, str, str, list[dict]]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
    if '"session_meta"' in first or path.name.startswith("rollout-"):
        return ("Codex",) + parse_codex(path)
    return ("Claude Code",) + parse_claude(path)


# ---------------------------------------------------------------- locating files

def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)) if p else ""


def project_root(cwd: str) -> Path:
    try:
        out = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd or ".").resolve()


def log_dir(cwd: str) -> Path:
    override = os.environ.get("ARIS_CONVERSATION_LOG_DIR")
    return Path(override) if override else project_root(cwd) / "conversation-logs"


def _cwd_of(path: Path) -> str:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i > 200:
                break
            m = re.search(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', line)
            if m:
                return json.loads(f'"{m.group(1)}"')
    return ""


def latest_transcript(codex: bool, cwd: str) -> Path | None:
    home = Path.home()
    pattern = (home / ".codex" / "sessions").glob("**/rollout-*.jsonl") if codex \
        else (home / ".claude" / "projects").glob("*/*.jsonl")
    want = _norm(cwd)
    for path in sorted(pattern, key=lambda p: p.stat().st_mtime, reverse=True):
        if _norm(_cwd_of(path)) == want:
            return path
    return None


def resolve_transcript(args) -> Path:
    if args.transcript:
        path = Path(args.transcript).expanduser()
    else:
        path = latest_transcript(args.codex, os.getcwd())
    if not path or not path.is_file():
        print("ERROR: transcript not found; pass --transcript <file.jsonl>", file=sys.stderr)
        sys.exit(2)
    return path


# ---------------------------------------------------------------- state and log

def _state_path(ldir: Path, session: str) -> Path:
    return ldir / ".state" / f"{session}.json"


def load_state(ldir: Path, session: str) -> dict:
    try:
        return json.loads(_state_path(ldir, session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"session_id": session, "logged_rounds": 0, "nagged_at": 0, "log_file": ""}


def save_state(ldir: Path, state: dict) -> None:
    path = _state_path(ldir, state["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _log_file(ldir: Path, state: dict, rounds: list[dict], tool: str, root: Path) -> Path:
    if state.get("log_file"):
        return ldir / state["log_file"]
    day = (rounds[0]["time"][:10] if rounds and rounds[0]["time"] else
           dt.date.today().isoformat()).replace("-", "")
    name = f"{day}_{state['session_id'][:8]}.md"
    state["log_file"] = name
    path = ldir / name
    if not path.exists():
        ldir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Conversation Log\n\n"
            f"- Session: `{state['session_id']}`\n- Tool: {tool}\n- Project: `{root}`\n"
            f"- Created: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            "- Rule: user messages are verbatim (copied from the transcript); "
            "agent replies are compressed to one line per round.\n\n---\n",
            encoding="utf-8", newline="\n")
    return path


def _quote(text: str) -> str:
    return "\n".join(("> " + ln) if ln else ">" for ln in text.splitlines()) or ">"


def _auto_summary(rnd: dict) -> str:
    text = rnd["texts"][-1] if rnd["texts"] else ""
    text = re.sub(r"[`*#>|]", "", re.sub(r"\s+", " ", text)).strip()
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0][:200] if text else "No text reply."
    if rnd["files"]:
        first += " Files: " + ", ".join(Path(f).name for f in rnd["files"][:5]) + "."
    return first + " (auto)"


def write_rounds(path: Path, rounds: list[dict], start: int, answers: dict[int, str]) -> None:
    end = start + len(rounds) - 1
    span = f"Round {start}" if start == end else f"Rounds {start}–{end}"
    lines = [f"\n## {span} · {rounds[0]['time']} → {rounds[-1]['time']}\n"]
    for i, rnd in enumerate(rounds, start):
        lines.append(f"**R{i}** · {rnd['time']}\nQ:\n{_quote(rnd['user'])}\nA: {answers[i].strip()}\n")
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def pending_payload(rounds: list[dict], start: int, session: str, transcript: Path) -> dict:
    out = []
    for i, rnd in enumerate(rounds, start):
        texts = rnd["texts"]
        earlier = " ".join(texts[:-1])
        out.append({
            "round": i, "time": rnd["time"], "user": rnd["user"],
            "agent_final_text": (texts[-1] if texts else "")[:2500],
            "agent_earlier_text": (earlier[:600] + " …") if len(earlier) > 600 else earlier,
            "tools": dict(rnd["tools"]), "files_changed": rnd["files"],
        })
    return {"session_id": session, "transcript": str(transcript), "first_round": start,
            "last_round": start + len(rounds) - 1, "rounds": out}


# ---------------------------------------------------------------- commands

def _context(transcript: Path):
    tool, session, cwd, rounds = parse(transcript)
    cwd = cwd or os.getcwd()
    ldir = log_dir(cwd)
    return tool, session, cwd, rounds, ldir, load_state(ldir, session)


def cmd_status(args) -> int:
    transcript = resolve_transcript(args)
    tool, session, cwd, rounds, ldir, state = _context(transcript)
    print(json.dumps({"session_id": session, "tool": tool, "transcript": str(transcript),
                      "log_dir": str(ldir), "log_file": state.get("log_file", ""),
                      "total_rounds": len(rounds), "logged_rounds": state["logged_rounds"],
                      "pending_rounds": len(rounds) - state["logged_rounds"], "every": EVERY},
                     indent=2))
    return 0


def _write_pending(transcript: Path, rounds, state, session, ldir, out: str | None) -> Path:
    start = state["logged_rounds"] + 1
    payload = pending_payload(rounds[start - 1:], start, session, transcript)
    path = Path(out) if out else ldir / ".state" / f"{session}_pending.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def cmd_pending(args) -> int:
    transcript = resolve_transcript(args)
    _, session, _, rounds, ldir, state = _context(transcript)
    if len(rounds) <= state["logged_rounds"]:
        print("Nothing to log: every round is already recorded.")
        return 1
    path = _write_pending(transcript, rounds, state, session, ldir, args.out)
    print(f"Pending rounds {state['logged_rounds'] + 1}-{len(rounds)} written to {path}")
    return 0


def cmd_append(args) -> int:
    transcript = resolve_transcript(args)
    tool, session, cwd, rounds, ldir, state = _context(transcript)
    raw = json.loads(Path(args.entries).read_text(encoding="utf-8"))
    items = raw.get("rounds", raw) if isinstance(raw, dict) else raw
    if isinstance(items, dict):
        answers = {int(k): str(v) for k, v in items.items()}
    else:
        answers = {int(e["round"]): str(e["a"]) for e in items}
    start = state["logged_rounds"] + 1
    if not answers or min(answers) != start:
        print(f"ERROR: entries must start at round {start} (the first unlogged round).", file=sys.stderr)
        return 1
    end = max(answers)
    missing = [i for i in range(start, end + 1) if not answers.get(i, "").strip()]
    if missing or end > len(rounds):
        print(f"ERROR: missing A-lines for rounds {missing}, or round {end} > {len(rounds)} rounds.",
              file=sys.stderr)
        return 1
    path = _log_file(ldir, state, rounds, tool, project_root(cwd))
    write_rounds(path, rounds[start - 1:end], start, answers)
    state["logged_rounds"] = end
    save_state(ldir, state)
    pending = ldir / ".state" / f"{session}_pending.json"
    if pending.exists():
        pending.unlink()
    print(f"Logged: rounds {start}-{end} to {path}")
    return 0


def flush(transcript: Path) -> str:
    tool, session, cwd, rounds, ldir, state = _context(transcript)
    start = state["logged_rounds"] + 1
    if len(rounds) < start:
        return "Nothing to log."
    chunk = rounds[start - 1:]
    answers = {i: _auto_summary(r) for i, r in enumerate(chunk, start)}
    path = _log_file(ldir, state, rounds, tool, project_root(cwd))
    write_rounds(path, chunk, start, answers)
    state["logged_rounds"] = len(rounds)
    save_state(ldir, state)
    return f"Logged: rounds {start}-{len(rounds)} (auto summaries) to {path}"


def cmd_flush(args) -> int:
    print(flush(resolve_transcript(args)))
    return 0


def cmd_hook(_args) -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        transcript = Path(data.get("transcript_path") or "")
        if not transcript.is_file():
            return 0
        event = data.get("hook_event_name", "Stop")
        if event == "SessionEnd":
            flush(transcript)
            return 0
        if data.get("stop_hook_active"):
            return 0
        _, session, _, rounds, ldir, state = _context(transcript)
        pending = len(rounds) - state["logged_rounds"]
        if pending < EVERY or state.get("nagged_at") == len(rounds):
            return 0
        state["nagged_at"] = len(rounds)
        save_state(ldir, state)
        pfile = _write_pending(transcript, rounds, state, session, ldir, None)
        entries = pfile.with_name(f"{session}_entries.json")
        reason = (
            f"Conversation log: {pending} rounds are not logged yet. Use the conversation-logger "
            f"skill now. Read {pfile.as_posix()}. Write one compressed A-line per round to "
            f"{entries.as_posix()} as {{\"rounds\": {{\"<n>\": \"<summary>\"}}}}. Then run: "
            f"python \"{SCRIPT.as_posix()}\" append --transcript \"{transcript.as_posix()}\" "
            f"--entries \"{entries.as_posix()}\". Do not copy user messages; the script does that. "
            "After it prints 'Logged:', stop without other output."
        )
        print(json.dumps({"decision": "block", "reason": reason}))
    except Exception as exc:  # never break the user's session
        print(f"conversation-logger hook skipped: {exc}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "pending", "append", "flush"):
        p = sub.add_parser(name)
        p.add_argument("--transcript", help="transcript .jsonl (default: newest for this folder)")
        p.add_argument("--codex", action="store_true", help="look for the newest Codex session")
        if name == "pending":
            p.add_argument("--out", help="output JSON path")
        if name == "append":
            p.add_argument("--entries", required=True, help='JSON: {"rounds": {"<n>": "<summary>"}}')
    sub.add_parser("hook")
    args = ap.parse_args()
    return {"status": cmd_status, "pending": cmd_pending, "append": cmd_append,
            "flush": cmd_flush, "hook": cmd_hook}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
