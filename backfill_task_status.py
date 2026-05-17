#!/usr/bin/env python3
"""
Claude Board — one-shot backfill
─────────────────────────────────
扫 ~/.claude/projects/*/*.jsonl，重放每个 Claude Code 会话里的
TaskCreate / TaskUpdate 工具调用，反推每条任务的最终状态，
然后把 ~/.claude-board/board.db 里 stuck 在 pending 的历史行
（多数是 task_id 形如 'tc-<md5>' 的 hash 兜底行）按内容匹配修正。

只更新 DB 中已存在、且状态不正确的行；不新增、不删除。
对已修好的新会话（task_id 是纯数字）幂等无操作。

用法：
  python backfill_task_status.py                # 全量扫描 + 改写
  python backfill_task_status.py --dry-run      # 只报会改什么
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

DB_PATH = Path.home() / ".claude-board" / "board.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
DAEMON_URL = os.environ.get("CLAUDE_BOARD_URL", "http://localhost:7820")


def post_event(event: dict, timeout: float = 5.0) -> bool:
    try:
        req = urllib.request.Request(
            f"{DAEMON_URL}/api/event",
            data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception as e:
        print(f"  ! POST failed: {e}", file=sys.stderr)
        return False

TASK_NUM_RE = re.compile(r"Task\s*#?(\d+)", re.IGNORECASE)


def _text_of(result_content) -> str:
    """tool_result 的 content 可能是 str / list[{type:text,text:...}] / dict。"""
    if isinstance(result_content, str):
        return result_content
    if isinstance(result_content, list):
        parts = []
        for blk in result_content:
            if isinstance(blk, dict):
                t = blk.get("text") or blk.get("content")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    if isinstance(result_content, dict):
        for k in ("text", "content", "output", "message", "result"):
            v = result_content.get(k)
            if isinstance(v, str):
                return v
    return ""


def _extract_created_id(tool_use_id: str, tool_results: dict, tool_response_inline: dict | None) -> str:
    """优先用 tool_response 内联的 task.id（新 Claude Code），fallback 去 tool_result 文字里找 Task #N。"""
    if isinstance(tool_response_inline, dict):
        task = tool_response_inline.get("task")
        if isinstance(task, dict):
            for k in ("id", "taskId", "task_id"):
                v = task.get(k)
                if v not in (None, ""):
                    return str(v)
        for k in ("taskId", "task_id", "id"):
            v = tool_response_inline.get(k)
            if v not in (None, ""):
                return str(v)
    raw = tool_results.get(tool_use_id)
    if raw is None:
        return ""
    m = TASK_NUM_RE.search(_text_of(raw))
    return m.group(1) if m else ""


def replay_session(jsonl_path: Path) -> dict:
    """返回 {numeric_id: {'content': str, 'status': str}}。
    status 可能是 pending / in_progress / completed；deleted 直接从字典剔除。"""
    tool_uses_ordered: list[tuple[str, str, dict, dict | None]] = []
    tool_results: dict[str, object] = {}

    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                msg = entry.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "tool_use":
                        name = blk.get("name", "")
                        if name in ("TaskCreate", "TaskUpdate"):
                            tool_uses_ordered.append(
                                (blk.get("id", ""), name, blk.get("input") or {},
                                 blk.get("response") if isinstance(blk.get("response"), dict) else None)
                            )
                    elif btype == "tool_result":
                        tool_results[blk.get("tool_use_id", "")] = blk.get("content")
    except OSError:
        return {}

    state: dict[str, dict] = {}
    for tuid, name, inp, inline_resp in tool_uses_ordered:
        if name == "TaskCreate":
            subject = (inp.get("subject") or "").strip()
            if not subject:
                continue
            nid = _extract_created_id(tuid, tool_results, inline_resp)
            if not nid:
                continue
            state[nid] = {"content": subject, "status": "pending"}
        elif name == "TaskUpdate":
            nid = str(inp.get("taskId") or "").strip()
            if not nid:
                continue
            status = inp.get("status")
            subject = (inp.get("subject") or "").strip()
            if status == "deleted":
                state.pop(nid, None)
                continue
            if nid not in state:
                # 没看见 TaskCreate，仍然记一下（孤儿 update）
                state[nid] = {"content": subject or f"Task #{nid}", "status": "pending"}
            if status:
                state[nid]["status"] = status
            if subject:
                state[nid]["content"] = subject
    return state


def find_jsonl_for_session(sid: str) -> Path | None:
    for p in PROJECTS_DIR.glob(f"*/{sid}.jsonl"):
        return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印会改的行，不写库")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 2

    # 只读 DB，写一律走 daemon HTTP API：
    #   1) 避免和 daemon 的写连接抢锁
    #   2) 自动触发 WS 广播，dashboard 立即看到效果
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row

    sessions = conn.execute("SELECT id, title, ended_at FROM sessions").fetchall()
    print(f"scanning {len(sessions)} sessions...")
    # 记录每个会话写之前是不是 ended——daemon 在收到任何 task_* 事件后会把
    # ended_at 清空（按设计是为活会话复活）。backfill 写完后要把它们标回去，
    # 否则历史会话会被一并"复活"挂在面板上。
    was_ended: dict[str, bool] = {s["id"]: bool(s["ended_at"]) for s in sessions}
    restopped: list[str] = []

    total_changed = 0
    sessions_touched = 0
    sessions_no_jsonl = 0
    sessions_no_replay = 0

    for s in sessions:
        sid = s["id"]
        jsonl = find_jsonl_for_session(sid)
        if jsonl is None:
            sessions_no_jsonl += 1
            continue
        replay = replay_session(jsonl)
        if not replay:
            sessions_no_replay += 1
            continue

        rows = conn.execute(
            "SELECT task_id, content, status FROM tasks WHERE session_id=?",
            (sid,)
        ).fetchall()
        by_id      = {r["task_id"]: dict(r) for r in rows}
        by_content = {(r["content"] or "").strip(): dict(r) for r in rows}

        # plan：(target_task_id, content, old_status_or_None, new_status, action)
        # action ∈ {"update", "insert"}
        plan: list[tuple[str, str, str | None, str, str]] = []
        for nid, v in replay.items():
            new_content = v["content"]
            new_status  = v["status"]
            # 1) 精确 task_id 命中（处理 daemon 兜底占位 / 已正确的新行）
            r = by_id.get(nid)
            if r:
                if r["status"] != new_status or (r["content"] or "").strip() != new_content.strip():
                    plan.append((nid, new_content, r["status"], new_status, "update"))
                continue
            # 2) 按 content 命中老 tc-<hash> 行
            r = by_content.get(new_content.strip())
            if r:
                if r["status"] != new_status:
                    plan.append((r["task_id"], new_content, r["status"], new_status, "update"))
                continue
            # 3) DB 里压根没这条 —— 历史 TaskCreate 当时被 hash 兜底然后被我清掉了，补回来
            plan.append((nid, new_content, None, new_status, "insert"))

        if not plan:
            continue

        sessions_touched += 1
        title_safe = (s["title"] or "").encode("ascii", "replace").decode()[:30]
        print(f"\n[{sid[:8]}] {title_safe}  -- {len(plan)} row(s)")
        for tid, c, old, new, action in plan:
            csafe = (c or "").encode("ascii", "replace").decode()[:55]
            old_disp = old or "(none)"
            print(f"  [{action:<6}] {tid:<14} {old_disp:<12} -> {new:<12} {csafe}")

        total_changed += len(plan)
        if not args.dry_run:
            for tid, content, _old, new, _action in plan:
                # task_upsert：existing→更新 content+status，missing→insert（daemon 兜底分支带真实 content）
                post_event({
                    "type":       "task_upsert",
                    "session_id": sid,
                    "task_id":    tid,
                    "content":    content,
                    "status":     new,
                })
            # 复位 ended_at：之前已 ended 的会话，写完后再标一次 stop
            if was_ended.get(sid):
                post_event({"type": "session_stop", "session_id": sid})
                restopped.append(sid)

    print()
    print(f"sessions with no JSONL transcript: {sessions_no_jsonl}")
    print(f"sessions with JSONL but no Task* calls: {sessions_no_replay}")
    print(f"sessions touched: {sessions_touched}")
    print(f"rows {'would be' if args.dry_run else ''} updated: {total_changed}")
    print()
    if restopped:
        print(f"re-marked {len(restopped)} previously-ended session(s) back to ended after writes")
    if args.dry_run:
        print("(dry-run, nothing written. Re-run without --dry-run to apply.)")
    else:
        print("Done. Dashboard should refresh via WebSocket within ~1s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
