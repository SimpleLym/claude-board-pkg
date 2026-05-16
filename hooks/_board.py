"""
共享辅助：
  1. debug_log()    — CLAUDE_BOARD_DEBUG_LOG 开关，写 payload 到文件
  2. is_session_start_phantom() — 仅判断"SessionStart 该不该建新卡"
       适用：SessionStart（subagent / slash / print 类的子进程不该建主卡）
  3. effective_session_id() — 返回应该归属的 session_id
       原则：subagent 的 task/message/stop 事件归到 parent session 名下，
       让 Claude 用 Agent 工具时的子任务能"流回主卡"，而不是另开一张卡。
"""

import json
import os
from datetime import datetime
from pathlib import Path


def debug_log(stage: str, payload: dict, extra: dict | None = None):
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


# SessionStart 时这些 source/kind 不该建主卡（避免幽灵）
_PHANTOM_SOURCES = {"subagent", "slash", "print", "agent", "compact"}
_PHANTOM_KINDS   = {"subagent", "agent"}


def _parent_id(payload: dict):
    for k in ("parent_session_id", "parentSessionId", "parent_id"):
        v = payload.get(k)
        if v:
            return str(v)
    return None


def is_session_start_phantom(payload: dict) -> tuple[bool, str]:
    """SessionStart 专用：是否是幽灵子会话（不该建主卡）。"""
    src = (payload.get("source") or "").lower()
    if src in _PHANTOM_SOURCES:
        return True, f"source={src}"
    kind = (payload.get("kind") or payload.get("session_kind") or "").lower()
    if kind in _PHANTOM_KINDS:
        return True, f"kind={kind}"
    pid = _parent_id(payload)
    if pid:
        return True, f"parent={pid}"
    if payload.get("is_subagent") or payload.get("isSubagent"):
        return True, "is_subagent=true"
    return False, ""


def effective_session_id(payload: dict) -> str:
    """活动事件（message / task_*）专用：如果是 subagent 触发的，
    返回 parent session_id；否则返回自身。让所有子事件"流回主卡"。"""
    return (_parent_id(payload)
            or (payload.get("session_id") or "").strip()
            or os.environ.get("CLAUDE_SESSION_ID", "").strip())
