#!/usr/bin/env python3
"""
Claude Board — One-time setup
─────────────────────────────
Installs daemon + hooks to ~/.claude-board/ and registers hooks at the
USER level (~/.claude/settings.json) so every Claude Code terminal on
the machine is auto-tracked by the dashboard.
"""

import json
import os
import shutil
import stat
import sys
from pathlib import Path

SKILL_DIR  = Path(__file__).parent
BOARD_DIR  = Path.home() / ".claude-board"
HOOKS_DST  = BOARD_DIR / "hooks"
HOOK_FILES = [
    "_board.py",          # 共享辅助：debug 日志 + subagent 过滤
    "session_start.py",
    "user_prompt_submit.py",
    "post_tool_use.py",
    "stop.py",
]

IS_WIN = os.name == "nt"
PYTHON_CMD = "python" if IS_WIN else "python3"


def cp_file(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    try:
        dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
    except Exception:
        pass
    print(f"  + {dst}")


def install_files():
    print("\n[1] Installing files...")
    cp_file(SKILL_DIR / "daemon.py",      BOARD_DIR / "daemon.py")
    cp_file(SKILL_DIR / "dashboard.html", BOARD_DIR / "dashboard.html")
    cp_file(SKILL_DIR / "tasks.html",     BOARD_DIR / "tasks.html")
    HOOKS_DST.mkdir(parents=True, exist_ok=True)
    for h in HOOK_FILES:
        src = SKILL_DIR / "hooks" / h
        if src.exists():
            cp_file(src, HOOKS_DST / h)


def _hook_entry(script_name: str, matcher: str | None = None) -> dict:
    script_path = HOOKS_DST / script_name
    cmd = f'{PYTHON_CMD} "{script_path}"'
    entry: dict = {"hooks": [{"type": "command", "command": cmd}]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def _find_existing(hooks_list: list, marker: str):
    """返回包含我们 hook script 的那一项（带 matcher / hooks 子结构），找不到返回 None。"""
    for h in hooks_list:
        for hh in h.get("hooks", []):
            if marker in hh.get("command", ""):
                return h
    return None


def install_hooks():
    """Register hooks at the USER level so every Claude Code terminal on
    this machine is auto-tracked. Falls back to project level if --project."""
    project_mode = "--project" in sys.argv

    if project_mode:
        claude_dir = Path.cwd() / ".claude"
        settings_f = claude_dir / "settings.local.json"
        scope = "project"
    else:
        claude_dir = Path.home() / ".claude"
        settings_f = claude_dir / "settings.json"
        scope = "user"

    claude_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[2] Configuring Claude Code hooks ({scope} level)...")

    cfg: dict = {}
    if settings_f.exists():
        try:
            cfg = json.loads(settings_f.read_text(encoding="utf-8"))
        except Exception:
            print(f"  ! could not parse existing {settings_f}; starting fresh")
            cfg = {}

    hooks_cfg = cfg.setdefault("hooks", {})

    plan = [
        ("SessionStart",     "session_start.py",      None),
        ("UserPromptSubmit", "user_prompt_submit.py", None),
        # 同一脚本，匹配多个工具：todo/任务工具 + Edit/Write（捕获 TASKS.md 改动）
        ("PostToolUse",      "post_tool_use.py",      "TodoWrite|TaskCreate|TaskUpdate|Edit|Write"),
        ("Stop",             "stop.py",               None),
    ]

    added = 0
    updated = 0
    for event, script, matcher in plan:
        lst = hooks_cfg.setdefault(event, [])
        marker = script  # filename uniquely identifies our hook
        existing = _find_existing(lst, marker)
        if existing is None:
            lst.append(_hook_entry(script, matcher))
            added += 1
        else:
            # 即便条目已存在，也强制把 matcher 更新到最新——避免老版本
            # matcher（如 "TodoWrite"）漏掉新加的 TaskCreate / Edit 等工具
            new_matcher = matcher
            old_matcher = existing.get("matcher")
            if old_matcher != new_matcher:
                if new_matcher is None:
                    existing.pop("matcher", None)
                else:
                    existing["matcher"] = new_matcher
                updated += 1

    settings_f.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                          encoding="utf-8")
    print(f"  + {settings_f}  (+{added} new, ~{updated} matcher updated)")


def check_deps():
    print("\n[3] Checking dependencies...")
    try:
        import aiohttp  # noqa: F401
        print(f"  + aiohttp {aiohttp.__version__}")
    except ImportError:
        print("  ! aiohttp not found, installing...")
        os.system(f'"{sys.executable}" -m pip install aiohttp -q')


def print_next_steps():
    print(f"""
{'=' * 60}
  Claude Board — Setup Complete

  1) START THE DAEMON (keep it running in a separate terminal):
       {PYTHON_CMD} "{BOARD_DIR / 'daemon.py'}"

  2) OPEN THE DASHBOARD:
       http://localhost:7820

  3) From now on, EVERY Claude Code terminal on this machine is
     auto-tracked. The board will show:
       - session sidebar (one entry per terminal)
       - live task panel (todo / doing / done + % done)
       - conversation log (each round folds open/closed)

  Re-run with  --project  to install hooks in just the current repo
  instead of user-wide.
{'=' * 60}
""")


if __name__ == "__main__":
    print("Claude Board Setup")
    install_files()
    install_hooks()
    check_deps()
    print_next_steps()
