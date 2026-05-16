"""
共享辅助：所有 hook 都需要的两件事
  1. 调试日志：环境变量 CLAUDE_BOARD_DEBUG_LOG 指向一个文件路径，
     hook 会把收到的 payload 完整追加进去。无 env var 则零开销。
  2. is_subagent / is_phantom：判断是否是不该上报的"幻象会话"
     （subagent、slash 命令、临时 print 子进程等）。
"""

import json
import os
from datetime import datetime
from pathlib import Path


def debug_log(stage: str, payload: dict, extra: dict | None = None):
    """开关：环境变量 CLAUDE_BOARD_DEBUG_LOG = <文件路径>。"""
    log = os.environ.get("CLAUDE_BOARD_DEBUG_LOG")
    if not log:
        return
    try:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} "
                    f"[{stage}] pid={os.getpid()} ===\n")
            if extra:
                f.write(json.dumps(extra, ensure_ascii=False) + "\n")
            f.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 已知的 source / kind 值——任何包含这些的会话都被认为不该上报
_SKIP_SOURCES = {"subagent", "slash", "print", "agent", "compact"}
_SKIP_KINDS   = {"subagent", "agent"}


def should_skip(payload: dict) -> tuple[bool, str]:
    """返回 (skip?, reason)。reason 用于调试日志。"""
    src = (payload.get("source") or "").lower()
    if src in _SKIP_SOURCES:
        return True, f"source={src}"

    kind = (payload.get("kind") or payload.get("session_kind") or "").lower()
    if kind in _SKIP_KINDS:
        return True, f"kind={kind}"

    # 任何带"父会话"标识的都是子会话
    for k in ("parent_session_id", "parentSessionId", "parent_id"):
        v = payload.get(k)
        if v:
            return True, f"{k}={v}"

    # SessionStart 显式标记
    if payload.get("is_subagent") or payload.get("isSubagent"):
        return True, "is_subagent=true"

    return False, ""
