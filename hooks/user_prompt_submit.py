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

sys.path.insert(0, str(Path(__file__).parent))
from _board import debug_log, effective_session_id   # noqa: E402

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
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8") if raw else "{}")
    except Exception:
        return

    # subagent 的 prompt 也归到 parent（如果有）
    session_id = effective_session_id(payload).strip()
    debug_log("UserPromptSubmit", payload, {"effective_sid": session_id})
    if not session_id:
        return

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return

    if is_slash_command_artifact(prompt):
        return

    cwd = (payload.get("cwd")
           or os.environ.get("CLAUDE_PROJECT_DIR")
           or os.getcwd())

    title = derive_title(prompt)

    # 顺序很关键：message 是创建行的活动事件，先发并把 title 塞进去，
    # daemon 创建行时直接用这个标题，避免"Untitled → 真实标题"那一闪。
    post_event({
        "type":          "message",
        "session_id":    session_id,
        "role":          "user",
        "content":       prompt[:MAX_USER_CHARS],
        "title_default": title,         # 创建行时用作初始 title
        "project":       cwd,           # 创建行时用作 project
    })

    # 若行已存在且 title 还是 'Untitled'（比如老会话被 resume），补一刀更新
    post_event({
        "type":       "session_title_default",
        "session_id": session_id,
        "title":      title,
    })


if __name__ == "__main__":
    main()
