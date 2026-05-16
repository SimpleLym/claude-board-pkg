#!/usr/bin/env python3
"""
Claude Board - UserPromptSubmit hook
─────────────────────────────────────
Fires every time the user submits a prompt.
Posts the prompt to the dashboard as a 'user' message and, on the
first prompt of a session, derives a default title from it.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

DAEMON = os.environ.get("CLAUDE_BOARD_URL", "http://localhost:7820")
MAX_USER_CHARS = 4000

# Claude Code 内建 slash 命令——触发 UserPromptSubmit 时 prompt 是裸命令名（无 /）。
# 这些不该建新会话卡。已知列表（持续扩展即可）。
_SLASH_CMDS = {
    "usage", "help", "clear", "compact", "init", "model", "ide", "mcp",
    "doctor", "status", "config", "settings", "cost", "memory", "permissions",
    "review", "continue", "resume", "exit", "quit", "login", "logout",
    "release-notes", "vim", "bug", "feedback", "agents", "hooks",
}


def is_slash_command_artifact(prompt: str) -> bool:
    """启发式：单行 / 短小 / 首词在已知 slash 名单内，就当作 slash 命令副产品。"""
    t = prompt.strip().lstrip("/")
    if not t or "\n" in t:
        return False
    first = t.split()[0].lower()
    return first in _SLASH_CMDS


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


def derive_title(prompt: str) -> str:
    text = " ".join(prompt.replace("\n", " ").split())
    return (text[:60] + "…") if len(text) > 60 else text


def main():
    try:
        # 必须读 buffer 再显式 UTF-8 解码：Windows 上 sys.stdin 默认 cp936/GBK，
        # 会把 Claude Code 传入的 UTF-8 JSON 解坏，导致中文乱码进库。
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8") if raw else "{}")
    except Exception:
        return

    session_id = (payload.get("session_id")
                  or os.environ.get("CLAUDE_SESSION_ID", "")).strip()
    if not session_id:
        return

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return

    # 跳过内建 slash 命令（如 /usage /help），它们会带来"幽灵会话"
    if is_slash_command_artifact(prompt):
        return

    cwd = (payload.get("cwd")
           or os.environ.get("CLAUDE_PROJECT_DIR")
           or os.getcwd())

    post_event({
        "type":       "session_init",
        "session_id": session_id,
        "project":    cwd,
    })

    post_event({
        "type":       "session_title_default",
        "session_id": session_id,
        "title":      derive_title(prompt),
    })

    post_event({
        "type":       "message",
        "session_id": session_id,
        "role":       "user",
        "content":    prompt[:MAX_USER_CHARS],
    })


if __name__ == "__main__":
    main()
