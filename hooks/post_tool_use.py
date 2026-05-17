#!/usr/bin/env python3
"""
Claude Board - PostToolUse hook
────────────────────────────────
处理三类工具：
  - TodoWrite             : 一次性整体 sync（老协议）
  - TaskCreate            : 单条新增 → upsert
  - TaskUpdate            : 单条状态变更 → upsert
  - Edit / Write          : 若目标是 TASKS.md，触发 plan_scan
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _board import debug_log, effective_session_id   # noqa: E402

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


def _ensure_session(sid: str, project: str):
    post_event({"type": "session_init", "session_id": sid, "project": project})


def _handle_todowrite(sid: str, project: str, payload: dict):
    tool_input = payload.get("tool_input") or {}
    todos = tool_input.get("todos", [])
    tasks = [
        {
            "id":       t.get("id", ""),
            "content":  (t.get("content") or "").strip(),
            "status":   t.get("status", "pending"),
            "priority": t.get("priority", "medium"),
        }
        for t in todos
        if (t.get("content") or "").strip()
    ]
    if not tasks:
        return
    _ensure_session(sid, project)
    post_event({"type": "task_sync", "session_id": sid, "tasks": tasks})


_TASK_NUM_RE = re.compile(r"Task\s*#?(\d+)", re.IGNORECASE)


def _extract_task_id(tool_resp) -> str:
    """TaskCreate 的 tool_response 形态会变，按已知顺序兜底：
      - {"task": {"id": "3", ...}}                  ← 当前 Claude Code 实际形态
      - {"taskId": "1"} / {"id": 1} / {"task_id": "1"}（结构化）
      - {"content"|"text"|"output"|"result"|"message": "Task #1 created ..."}
      - "Task #1 created successfully: ..."         ← 早期纯文本
    必须拿到和 TaskUpdate(taskId=...) 一致的 id，否则后续状态更新对不上号。"""
    if isinstance(tool_resp, dict):
        task = tool_resp.get("task")
        if isinstance(task, dict):
            for k in ("id", "taskId", "task_id"):
                v = task.get(k)
                if v not in (None, ""):
                    return str(v)
        for k in ("taskId", "task_id", "id"):
            v = tool_resp.get(k)
            if v not in (None, ""):
                return str(v)
        for k in ("content", "text", "output", "result", "message"):
            v = tool_resp.get(k)
            if isinstance(v, str):
                m = _TASK_NUM_RE.search(v)
                if m:
                    return m.group(1)
            elif isinstance(v, list):
                for blk in v:
                    if isinstance(blk, dict):
                        txt = blk.get("text") or blk.get("content") or ""
                        if isinstance(txt, str):
                            m = _TASK_NUM_RE.search(txt)
                            if m:
                                return m.group(1)
    elif isinstance(tool_resp, str):
        m = _TASK_NUM_RE.search(tool_resp)
        if m:
            return m.group(1)
    return ""


def _handle_task_create(sid: str, project: str, payload: dict):
    tool_input = payload.get("tool_input") or {}
    tool_resp  = payload.get("tool_response") or {}

    subject = (tool_input.get("subject") or "").strip()
    if not subject:
        return

    tid = _extract_task_id(tool_resp)
    if not tid:
        # 兜底：用 subject 的 hash 当 id；老路径 —— TaskUpdate 找不到时 daemon 会自动建占位行
        import hashlib
        tid = "tc-" + hashlib.md5(subject.encode("utf-8")).hexdigest()[:10]

    _ensure_session(sid, project)
    post_event({
        "type": "task_upsert", "session_id": sid,
        "task_id": tid, "content": subject, "status": "pending",
    })


def _handle_task_update(sid: str, project: str, payload: dict):
    tool_input = payload.get("tool_input") or {}
    tid = str(tool_input.get("taskId") or "").strip()
    if not tid:
        return

    status = tool_input.get("status")  # 可能是 None（只改 subject 等）
    subject = (tool_input.get("subject") or "").strip()

    # deleted 走单独的删除事件，避免把 "deleted" 当成第 4 种状态留在看板上
    if status == "deleted":
        _ensure_session(sid, project)
        post_event({"type": "task_delete", "session_id": sid, "task_id": tid})
        return

    event = {"type": "task_upsert", "session_id": sid, "task_id": tid}
    if status:
        event["status"] = status
    if subject:
        event["content"] = subject
    if len(event) <= 3:  # 只有 type/sid/task_id —— 没什么可同步
        return
    _ensure_session(sid, project)
    post_event(event)


def _handle_edit(sid: str, project: str, payload: dict):
    """Edit / Write 命中 TASKS.md → 立即触发 plan_scan，秒级同步。"""
    tool_input = payload.get("tool_input") or {}
    fp = str(tool_input.get("file_path") or "")
    if not fp.lower().endswith("tasks.md"):
        return
    post_event({"type": "plan_scan", "project": project})


HANDLERS = {
    "TodoWrite":  _handle_todowrite,
    "TaskCreate": _handle_task_create,
    "TaskUpdate": _handle_task_update,
    "Edit":       _handle_edit,
    "Write":      _handle_edit,
}


def main():
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8") if raw else "{}")
    except Exception:
        return

    # 关键：subagent 触发的任务/编辑事件，要归到 parent session 名下，
    # 让 Claude 在主对话里用 Agent 工具开子任务时，进度仍流回主卡。
    session_id = effective_session_id(payload).strip()
    debug_log("PostToolUse", payload,
              {"tool": payload.get("tool_name"), "effective_sid": session_id})
    if not session_id:
        return

    tool_name = payload.get("tool_name", "")
    handler = HANDLERS.get(tool_name)
    if not handler:
        return

    project = (payload.get("cwd")
               or os.environ.get("CLAUDE_PROJECT_DIR")
               or os.getcwd())

    try:
        handler(session_id, project, payload)
    except Exception:
        pass


if __name__ == "__main__":
    main()
