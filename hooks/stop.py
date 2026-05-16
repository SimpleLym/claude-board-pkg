#!/usr/bin/env python3
"""
Claude Board - Stop hook
─────────────────────────
Fires when Claude finishes responding to a user prompt.
Parses transcript_path (JSONL) to find the most recent assistant turn,
concatenates its text blocks, and posts a 'assistant' message event so
the dashboard's conversation log shows the response (folded by default).
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _board import debug_log, should_skip   # noqa: E402

DAEMON = os.environ.get("CLAUDE_BOARD_URL", "http://localhost:7820")
MAX_ASSISTANT_CHARS = 4000


def post_event(event: dict, timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(
            f"{DAEMON}/api/event",
            data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def extract_usage_totals(transcript_path: str) -> dict:
    """走一遍 JSONL，累加所有 assistant 条目的 token 使用量。
    last_context_tokens 取最后一条 assistant 的 input_tokens——
    它代表当前对话累积的上下文窗口大小。"""
    p = Path(transcript_path)
    if not p.exists():
        return {}
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}

    it = ot = cr = cc = 0
    last_ctx = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        u = msg.get("usage") or {}
        if not u:
            continue
        it += int(u.get("input_tokens") or 0)
        ot += int(u.get("output_tokens") or 0)
        cr += int(u.get("cache_read_input_tokens") or 0)
        cc += int(u.get("cache_creation_input_tokens") or 0)
        # 上下文 = 该轮输入 + cache_read + cache_creation
        last_ctx = (int(u.get("input_tokens") or 0)
                    + int(u.get("cache_read_input_tokens") or 0)
                    + int(u.get("cache_creation_input_tokens") or 0))
    return {
        "input_tokens": it,
        "output_tokens": ot,
        "cache_read_tokens": cr,
        "cache_creation_tokens": cc,
        "last_context_tokens": last_ctx,
    }


def extract_last_assistant_text(transcript_path: str) -> str:
    """Walk the JSONL transcript backwards, collect text from the most
    recent contiguous block of assistant entries (a single response can be
    split across multiple JSONL lines), stop on the previous user turn."""
    p = Path(transcript_path)
    if not p.exists():
        return ""

    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""

    chunks: list[str] = []
    saw_assistant = False
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue

        etype = entry.get("type")
        if etype == "assistant":
            saw_assistant = True
            msg = entry.get("message") or {}
            content = msg.get("content")
            text_pieces: list[str] = []
            if isinstance(content, str):
                text_pieces.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_pieces.append(block.get("text", ""))
            if text_pieces:
                chunks.append("\n".join(t for t in text_pieces if t).strip())
        elif etype == "user":
            if saw_assistant:
                break
        # ignore tool_use / tool_result / system entries

    chunks.reverse()
    return "\n\n".join(c for c in chunks if c).strip()


def main():
    try:
        # 必须读 buffer 再显式 UTF-8 解码：Windows 上 sys.stdin 默认 cp936/GBK，
        # 会把 Claude Code 传入的 UTF-8 JSON 解坏，导致中文乱码进库。
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8") if raw else "{}")
    except Exception:
        payload = {}

    debug_log("Stop", payload)

    session_id = (payload.get("session_id")
                  or os.environ.get("CLAUDE_SESSION_ID", "")).strip()
    if not session_id:
        return

    skip, reason = should_skip(payload)
    if skip:
        debug_log("Stop-SKIPPED", payload, {"reason": reason})
        return

    # 兜底找 transcript：先看 payload 已知字段，再 env var，最后按 session_id
    # 在 ~/.claude/projects/*/*.jsonl 里 glob（Claude Code 不同版本字段名可能不同）
    transcript_path = (payload.get("transcript_path")
                       or payload.get("transcriptPath")
                       or os.environ.get("CLAUDE_TRANSCRIPT_PATH", ""))
    if not transcript_path and session_id:
        try:
            base = Path.home() / ".claude" / "projects"
            for p in base.glob(f"*/{session_id}.jsonl"):
                transcript_path = str(p)
                break
        except Exception:
            pass
    debug_log("Stop-resolved", {"session_id": session_id, "transcript_path": transcript_path})

    cwd = (payload.get("cwd")
           or os.environ.get("CLAUDE_PROJECT_DIR")
           or os.getcwd())

    # Ensure session is registered before posting the message.
    post_event({
        "type":       "session_init",
        "session_id": session_id,
        "project":    cwd,
    })

    text = extract_last_assistant_text(transcript_path) if transcript_path else ""
    if text:
        post_event({
            "type":       "message",
            "session_id": session_id,
            "role":       "assistant",
            "content":    text[:MAX_ASSISTANT_CHARS],
        })

    # 顺手刷新 token usage（每轮 Claude 回复结束都更新一次）
    usage = extract_usage_totals(transcript_path) if transcript_path else {}
    if usage:
        post_event({
            "type":       "session_usage",
            "session_id": session_id,
            **usage,
        })


if __name__ == "__main__":
    main()
