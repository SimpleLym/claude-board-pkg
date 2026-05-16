#!/usr/bin/env python3
"""
Claude Board - SessionStart hook
─────────────────────────────────
Fires when a Claude Code session begins (start / resume / clear).
Registers the session on the dashboard so it appears in the sidebar
immediately, before any user message.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _board import debug_log, should_skip   # noqa: E402

DAEMON = os.environ.get("CLAUDE_BOARD_URL", "http://localhost:7820")


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


def main():
    try:
        # 必须读 buffer 再显式 UTF-8 解码：Windows 上 sys.stdin 默认 cp936/GBK，
        # 会把 Claude Code 传入的 UTF-8 JSON 解坏，导致中文乱码进库。
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8") if raw else "{}")
    except Exception:
        payload = {}

    debug_log("SessionStart", payload)

    session_id = (payload.get("session_id")
                  or os.environ.get("CLAUDE_SESSION_ID", "")).strip()
    if not session_id:
        return

    # 跳过 subagent / slash 命令 / 临时 print 子进程等"幻象会话"
    skip, reason = should_skip(payload)
    if skip:
        debug_log("SessionStart-SKIPPED", payload, {"reason": reason})
        return

    cwd = (payload.get("cwd")
           or os.environ.get("CLAUDE_PROJECT_DIR")
           or os.getcwd())

    # Register the session; leave title as 'Untitled' so the first user
    # prompt (via UserPromptSubmit) can claim it with a descriptive title.
    post_event({
        "type":       "session_init",
        "session_id": session_id,
        "project":    cwd,
    })


if __name__ == "__main__":
    main()
