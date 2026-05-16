#!/usr/bin/env python3
"""
Claude Board Daemon
─────────────────────────────────────────────
HTTP + WebSocket server that receives events from Claude Code hooks
and broadcasts live state to the web dashboard.

  POST /api/event   ← hooks / Claude skill send events here
  GET  /api/state   ← full state snapshot
  WS   /ws          ← dashboard subscribes here
  GET  /            ← serve dashboard.html

Usage:
  python3 daemon.py             # starts on port 7820
  PORT=9000 python3 daemon.py   # custom port
"""

import asyncio
import json
import os
import re
import sys
import sqlite3
import hashlib
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta, timezone

try:
    import aiohttp
    from aiohttp import web, WSMsgType
except ImportError:
    print("Error: aiohttp is required. Install with:")
    print("  pip install aiohttp")
    raise

PORT = int(os.environ.get("CLAUDE_BOARD_PORT", "7820"))
DB_PATH = Path.home() / ".claude-board" / "board.db"
DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"
TASKS_PATH     = Path(__file__).parent / "tasks.html"

ws_clients: set = set()
_db_conn = None


# ─── Database ────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _init_db(_db_conn)
    return _db_conn


def _init_db(conn: sqlite3.Connection):
    conn.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS sessions (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL DEFAULT 'Untitled',
        project     TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        ended_at    TEXT
    );

    CREATE TABLE IF NOT EXISTS rounds (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL,
        round_num   INTEGER NOT NULL,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        timestamp   TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );
    CREATE INDEX IF NOT EXISTS idx_rounds_sid ON rounds(session_id, round_num);

    -- 老库迁移：ended_at 列若不存在则补上
    -- (SQLite 不支持 IF NOT EXISTS on ADD COLUMN，所以在 Python 侧处理)

    CREATE TABLE IF NOT EXISTS tasks (
        task_id     TEXT NOT NULL,
        session_id  TEXT NOT NULL,
        content     TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending',
        priority    TEXT NOT NULL DEFAULT 'medium',
        sort_order  INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (session_id, task_id)
    );

    -- 项目级规划：来自 <project>/TASKS.md 的解析结果
    CREATE TABLE IF NOT EXISTS plan_items (
        project     TEXT NOT NULL,
        item_id     TEXT NOT NULL,       -- 内容 hash，跨次解析稳定
        content     TEXT NOT NULL,
        status      TEXT NOT NULL,       -- pending / completed
        active      INTEGER NOT NULL DEFAULT 0,  -- 行内是否含 🔄
        indent      INTEGER NOT NULL DEFAULT 0,  -- 缩进级（用于父子）
        parent_id   TEXT,                -- 上一行更小缩进的 item_id
        group_label TEXT,                -- 所属 ## 二级标题
        sort_order  INTEGER NOT NULL DEFAULT 0,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (project, item_id)
    );

    -- 项目元数据：mtime 用于轮询变更检测
    CREATE TABLE IF NOT EXISTS plan_files (
        project     TEXT PRIMARY KEY,
        file_path   TEXT NOT NULL,
        last_mtime  REAL NOT NULL DEFAULT 0,
        last_scan   TEXT
    );
    """)
    # 老库迁移：补缺失列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    for col, ddl in [
        ("ended_at",              "TEXT"),
        ("input_tokens",          "INTEGER NOT NULL DEFAULT 0"),
        ("output_tokens",         "INTEGER NOT NULL DEFAULT 0"),
        ("cache_read_tokens",     "INTEGER NOT NULL DEFAULT 0"),
        ("cache_creation_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("last_context_tokens",   "INTEGER NOT NULL DEFAULT 0"),
        ("model",                 "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {ddl}")
    conn.commit()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ─── TASKS.md 解析 ───────────────────────────────────────────────────────────

# 形如：  - [ ] 任务内容
#        - [x] 已完成
#        前导空格 / 制表符决定缩进级
_TASK_LINE = re.compile(r"^(?P<indent>[ \t]*)-\s+\[(?P<mark>[ xX])\]\s+(?P<text>.+?)\s*$")
_GROUP_LINE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_FENCE_LINE = re.compile(r"^\s*(```|~~~)")   # 代码围栏 (``` 或 ~~~)
_ACTIVE_MARK = "🔄"


def _indent_level(spaces: str) -> int:
    """按 2 空格 / 1 Tab 折算为缩进级（0 起算）。"""
    expanded = spaces.replace("\t", "  ")
    return len(expanded) // 2


def parse_tasks_md(text: str) -> list[dict]:
    """解析 TASKS.md 内容，返回 [{content, status, active, indent, group, sort_order}]，
    顺序与原文一致。parent_id 由调用方在写库时根据 indent 链推断。"""
    items: list[dict] = []
    cur_group = ""
    order = 0
    in_fence = False   # ``` 或 ~~~ 包围的代码块内 → 跳过解析
    for line in text.splitlines():
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        m = _GROUP_LINE.match(line)
        if m:
            cur_group = m.group("title").strip()
            continue

        m = _TASK_LINE.match(line)
        if not m:
            continue

        text_part = m.group("text")
        active = _ACTIVE_MARK in text_part
        content = text_part.replace(_ACTIVE_MARK, "").strip()
        if not content:
            continue
        items.append({
            "content": content,
            "status": "completed" if m.group("mark").lower() == "x" else "pending",
            "active": active,
            "indent": _indent_level(m.group("indent")),
            "group": cur_group,
            "sort_order": order,
        })
        order += 1
    return items


def _stable_item_id(project: str, content: str, sort_order: int) -> str:
    """内容稳定 hash —— 同一行重命名后等同新任务。
    sort_order 防止同一项目内重复内容冲突。"""
    h = hashlib.md5(f"{project}|{sort_order}|{content}".encode("utf-8")).hexdigest()
    return h[:12]


def _find_tasks_md(project: str) -> Path | None:
    """约定：<project>/TASKS.md。"""
    if not project:
        return None
    try:
        p = Path(project) / "TASKS.md"
        return p if p.exists() else None
    except Exception:
        return None


def scan_project_plan(project: str) -> bool:
    """扫描某个项目的 TASKS.md，写入 plan_items；返回是否有内容变化。"""
    if not project:
        return False
    db = get_db()
    fp = _find_tasks_md(project)
    if fp is None:
        # 文件不存在：清空既有计划（防止文件被删后还残留）
        existed = db.execute(
            "SELECT 1 FROM plan_items WHERE project=? LIMIT 1", (project,)
        ).fetchone()
        if existed:
            db.execute("DELETE FROM plan_items WHERE project=?", (project,))
            db.execute("DELETE FROM plan_files WHERE project=?", (project,))
            db.commit()
            return True
        return False

    try:
        mtime = fp.stat().st_mtime
    except OSError:
        return False

    row = db.execute(
        "SELECT last_mtime FROM plan_files WHERE project=?", (project,)
    ).fetchone()
    if row and abs(row["last_mtime"] - mtime) < 1e-6:
        return False  # 没变

    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    items = parse_tasks_md(text)
    ts = now()

    # 推断 parent_id：按缩进栈
    stack: list[tuple[int, str]] = []  # [(indent, item_id), ...]
    rows = []
    for it in items:
        iid = _stable_item_id(project, it["content"], it["sort_order"])
        while stack and stack[-1][0] >= it["indent"]:
            stack.pop()
        parent = stack[-1][1] if stack else None
        rows.append((project, iid, it["content"], it["status"],
                     1 if it["active"] else 0,
                     it["indent"], parent, it["group"], it["sort_order"], ts))
        stack.append((it["indent"], iid))

    # 全量替换：先清空再写入。简单可靠，避免增量同步的脏数据。
    db.execute("DELETE FROM plan_items WHERE project=?", (project,))
    if rows:
        db.executemany("""
            INSERT INTO plan_items
                (project, item_id, content, status, active, indent,
                 parent_id, group_label, sort_order, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, rows)

    db.execute("""
        INSERT INTO plan_files (project, file_path, last_mtime, last_scan)
        VALUES (?,?,?,?)
        ON CONFLICT(project) DO UPDATE SET
            file_path=excluded.file_path,
            last_mtime=excluded.last_mtime,
            last_scan=excluded.last_scan
    """, (project, str(fp), mtime, ts))
    db.commit()
    return True


def delete_plan_line(project: str, item_id: str) -> bool:
    """从 TASKS.md 删掉对应的那一行（按 sort_order 定位，content 做 sanity check）。"""
    db = get_db()
    row = db.execute(
        "SELECT content, sort_order FROM plan_items WHERE project=? AND item_id=?",
        (project, item_id)
    ).fetchone()
    if not row:
        return False
    target_order = row["sort_order"]
    target_text  = row["content"]

    fp = _find_tasks_md(project)
    if fp is None:
        return False
    try:
        lines = fp.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return False

    in_fence = False
    seen = -1
    cut_idx = -1
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _TASK_LINE.match(line)
        if not m:
            continue
        seen += 1
        if seen == target_order:
            # sanity：内容里应包含目标文本（忽略 🔄）
            inline = m.group("text").replace(_ACTIVE_MARK, "").strip()
            if inline != target_text:
                return False
            cut_idx = i
            break

    if cut_idx < 0:
        return False

    del lines[cut_idx]
    try:
        fp.write_text("".join(lines), encoding="utf-8")
    except OSError:
        return False
    return True


def known_projects() -> list[str]:
    """从 sessions + plan_files 收集所有可能的项目目录。"""
    db = get_db()
    out = set()
    for r in db.execute("SELECT DISTINCT project FROM sessions WHERE project!=''"):
        out.add(r["project"])
    for r in db.execute("SELECT project FROM plan_files"):
        out.add(r["project"])
    return list(out)


# ─── State Serialization ─────────────────────────────────────────────────────

# 超过这个时长没有任何事件的 session，自动标 ended（处理直接关 shell 的情况）
SESSION_STALE_HOURS = 4

def _auto_end_stale_sessions(db: sqlite3.Connection) -> int:
    """把 4 小时没动静的活跃会话自动标 ended_at = updated_at。返回标了几条。"""
    cutoff = (datetime.now() - timedelta(hours=SESSION_STALE_HOURS)
              ).isoformat(timespec="seconds")
    cur = db.execute("""
        UPDATE sessions
        SET ended_at = updated_at
        WHERE ended_at IS NULL AND updated_at < ?
    """, (cutoff,))
    if cur.rowcount:
        db.commit()
    return cur.rowcount


def get_full_state() -> dict:
    db = get_db()
    _auto_end_stale_sessions(db)

    sessions = [dict(r) for r in db.execute(
        "SELECT * FROM sessions ORDER BY updated_at DESC"
    )]

    for s in sessions:
        # 全部对话（user/assistant 混合），用于卡片内可滚动会话时间线
        rows = [dict(r) for r in db.execute("""
            SELECT role, content, timestamp, round_num FROM rounds
            WHERE session_id=?
            ORDER BY round_num ASC, id ASC
        """, (s["id"],))]
        s["recent_rounds"] = rows   # 时间正序

        s["tasks"] = [dict(r) for r in db.execute(
            "SELECT * FROM tasks WHERE session_id=? ORDER BY sort_order ASC",
            (s["id"],)
        )]
        # 「是否做过规划」：只要这个会话曾经触发过 TodoWrite（tasks 表里有任意一行）
        # 就视为已规划，卡片才会显示进度条/进行中/已完成；否则只显示对话。
        s["has_plan"] = len(s["tasks"]) > 0

    # 项目级规划（TASKS.md）：按项目分组返回
    plans_rows = [dict(r) for r in db.execute("""
        SELECT p.project, p.item_id, p.content, p.status, p.active,
               p.indent, p.parent_id, p.group_label, p.sort_order, p.updated_at,
               f.file_path
        FROM plan_items p
        LEFT JOIN plan_files f ON f.project = p.project
        ORDER BY p.project ASC, p.sort_order ASC
    """)]
    plans_map: dict = {}
    for r in plans_rows:
        proj = r["project"]
        if proj not in plans_map:
            plans_map[proj] = {
                "project":   proj,
                "file_path": r["file_path"] or "",
                "items":     [],
            }
        plans_map[proj]["items"].append({
            "id":        r["item_id"],
            "content":   r["content"],
            "status":    r["status"],
            "active":    bool(r["active"]),
            "indent":    r["indent"],
            "parent_id": r["parent_id"],
            "group":     r["group_label"],
        })
    plans = list(plans_map.values())

    # Carryover: tasks updated BEFORE today that are still pending / in_progress.
    # Each row is enriched with its session's title / project so the dashboard
    # can render a stand-alone "yesterday board" without joining client-side.
    today = datetime.now().strftime("%Y-%m-%d")
    carryover = [dict(r) for r in db.execute("""
        SELECT t.task_id, t.session_id, t.content, t.status, t.priority,
               t.updated_at, s.title AS session_title, s.project AS session_project
        FROM tasks t
        JOIN sessions s ON s.id = t.session_id
        WHERE t.status IN ('pending', 'in_progress')
          AND substr(t.updated_at, 1, 10) < ?
        ORDER BY t.updated_at DESC, t.sort_order ASC
    """, (today,))]

    return {
        "sessions":     sessions,
        "carryover":    carryover,
        "plans":        plans,
        "usage_window": dict(_usage_window),
        "oauth_usage":  dict(_oauth_usage),
        "server_time":  now(),
    }


# ─── Event Processing ─────────────────────────────────────────────────────────

# 不需要 session_id 的"项目级"事件
_PROJECT_EVENTS = {"plan_scan", "plan_delete"}


def process_event(data: dict):
    db = get_db()
    etype = data.get("type", "")
    sid = (data.get("session_id") or "").strip()
    ts = now()

    # 项目级事件单独走，跳过 session_id 校验
    if etype in _PROJECT_EVENTS:
        project = (data.get("project") or "").strip()
        if not project:
            return False
        if etype == "plan_scan":
            return scan_project_plan(project)
        if etype == "plan_delete":
            item_id = (data.get("item_id") or "").strip()
            if not item_id:
                return False
            ok = delete_plan_line(project, item_id)
            if ok:
                scan_project_plan(project)
            return ok

    if not sid:
        return False

    # 只有"实际活动"事件才允许新建会话行：
    #   - message       用户/Claude 真发了消息
    #   - task_sync     真规划了任务
    #   - task_upsert   真创建/更新了任务
    # 像 session_init / session_title / session_stop / session_usage 这种
    # 纯生命周期事件，如果会话还不存在，就直接返回——避免 `claude -p` /
    # 一次性子进程留下"幽灵卡"。
    ACTIVITY_EVENTS = {"message", "task_sync", "task_upsert"}
    row_exists = db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() is not None
    if not row_exists:
        if etype not in ACTIVITY_EVENTS:
            return False
        # 创建行时，如果事件里捎带了 title_default（user_prompt_submit 会塞），
        # 就用它代替"Untitled"——这样第一次广播就有真实标题，看板上看不到闪。
        init_title = (data.get("title_default") or "").strip() or "Untitled"
        db.execute("""
            INSERT INTO sessions (id, title, project, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (sid, init_title, data.get("project", ""), ts, ts))

    changed = True

    if etype == "session_init":
        title = (data.get("title") or "").strip()
        project = (data.get("project") or "").strip()
        db.execute("""
            UPDATE sessions SET
                title   = CASE WHEN ? != '' THEN ? ELSE title END,
                project = CASE WHEN ? != '' THEN ? ELSE project END,
                updated_at = ?
            WHERE id = ?
        """, (title, title, project, project, ts, sid))
        # 注册项目即扫一次 TASKS.md（首次扫不阻塞响应；下面 commit 之前完成即可）
        if project:
            try:
                scan_project_plan(project)
            except Exception:
                pass

    elif etype == "session_title":
        title = (data.get("title") or "").strip()
        if title:
            db.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                (title, ts, sid)
            )

    elif etype == "session_title_default":
        # Only updates title if the current title is still 'Untitled'.
        title = (data.get("title") or "").strip()
        if title:
            db.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=? AND title='Untitled'",
                (title, ts, sid)
            )

    elif etype == "session_stop":
        # 标记会话已结束；如果该会话又来新事件（见下方"复活"逻辑），ended_at 会被清空。
        db.execute(
            "UPDATE sessions SET updated_at=?, ended_at=? WHERE id=?",
            (ts, ts, sid)
        )

    elif etype == "message":
        role = data.get("role", "user")
        content = (data.get("content") or "").strip()
        if not content:
            return False

        row = db.execute(
            "SELECT COALESCE(MAX(round_num), 0) AS mr FROM rounds WHERE session_id=?",
            (sid,)
        ).fetchone()
        max_round = row["mr"]

        round_num = (max_round + 1) if role == "user" else max(max_round, 1)

        db.execute("""
            INSERT INTO rounds (session_id, round_num, role, content, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (sid, round_num, role, content, ts))
        db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, sid))

    elif etype == "session_usage":
        try:
            it = int(data.get("input_tokens") or 0)
            ot = int(data.get("output_tokens") or 0)
            cr = int(data.get("cache_read_tokens") or 0)
            cc = int(data.get("cache_creation_tokens") or 0)
            ctx = int(data.get("last_context_tokens") or 0)
        except (TypeError, ValueError):
            return False
        model = (data.get("model") or "").strip() or None
        db.execute("""
            UPDATE sessions SET
                input_tokens          = ?,
                output_tokens         = ?,
                cache_read_tokens     = ?,
                cache_creation_tokens = ?,
                last_context_tokens   = ?,
                model                 = COALESCE(?, model),
                updated_at            = ?
            WHERE id = ?
        """, (it, ot, cr, cc, ctx, model, ts, sid))

    elif etype == "session_delete":
        # 物理删除整个会话（连同 tasks / rounds）
        c1 = db.execute("DELETE FROM rounds   WHERE session_id=?", (sid,)).rowcount
        c2 = db.execute("DELETE FROM tasks    WHERE session_id=?", (sid,)).rowcount
        c3 = db.execute("DELETE FROM sessions WHERE id=?",         (sid,)).rowcount
        db.commit()
        return c3 > 0 or c1 > 0 or c2 > 0

    elif etype == "task_delete":
        # 删除某 session 的一条任务
        tid = (data.get("task_id") or "").strip()
        if not tid:
            return False
        cur = db.execute(
            "DELETE FROM tasks WHERE session_id=? AND task_id=?",
            (sid, tid)
        )
        if cur.rowcount == 0:
            return False
        db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, sid))
        db.commit()
        return True

    elif etype == "task_upsert":
        # 单条 upsert（来自 TaskCreate / TaskUpdate hook）
        tid = (data.get("task_id") or "").strip()
        if not tid:
            return False
        content = (data.get("content") or "").strip()
        status  = data.get("status")
        existing = db.execute(
            "SELECT content, status FROM tasks WHERE session_id=? AND task_id=?",
            (sid, tid)
        ).fetchone()
        if existing:
            new_content = content or existing["content"]
            new_status  = status  or existing["status"]
            db.execute("""
                UPDATE tasks SET content=?, status=?, updated_at=?
                WHERE session_id=? AND task_id=?
            """, (new_content, new_status, ts, sid, tid))
        else:
            if not content:
                return False
            # 新插入：sort_order 用现有最大 + 1
            row = db.execute(
                "SELECT COALESCE(MAX(sort_order), -1)+1 AS n FROM tasks WHERE session_id=?",
                (sid,)
            ).fetchone()
            db.execute("""
                INSERT INTO tasks (task_id, session_id, content, status, priority,
                                   sort_order, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (tid, sid, content, status or "pending", "medium",
                  row["n"], ts, ts))
        db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, sid))

    elif etype == "task_sync":
        # Full replacement sync from TodoWrite
        tasks = data.get("tasks", [])
        for i, t in enumerate(tasks):
            raw_id = (t.get("id") or "").strip()
            content = (t.get("content") or "").strip()
            if not content:
                continue
            task_id = raw_id or hashlib.md5(content.encode()).hexdigest()[:10]
            status = t.get("status", "pending")
            priority = t.get("priority", "medium")

            db.execute("""
                INSERT INTO tasks
                    (task_id, session_id, content, status, priority, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, task_id) DO UPDATE SET
                    status     = excluded.status,
                    priority   = excluded.priority,
                    sort_order = excluded.sort_order,
                    updated_at = excluded.updated_at
            """, (task_id, sid, content, status, priority, i, ts, ts))

        db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, sid))

    else:
        # 未知事件类型——返回特殊哨兵让 route_event 给 400，避免前端"假成功"
        return None

    # 任何"非结束"事件，都视为会话复活，清空 ended_at
    if changed and etype != "session_stop":
        db.execute("UPDATE sessions SET ended_at=NULL WHERE id=?", (sid,))

    db.commit()
    return changed


# ─── HTTP Routes ──────────────────────────────────────────────────────────────

async def route_event(req: web.Request) -> web.Response:
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    # 异步事件单独处理（process_event 是同步的）
    if data.get("type") == "usage_refresh":
        ok = await fetch_oauth_usage()
        if ok:
            await broadcast({"type": "state_update", "state": get_full_state()})
        return web.json_response({
            "ok": ok, "changed": ok,
            "error": _oauth_usage.get("error"),
        })

    changed = process_event(data)

    if changed is None:
        return web.json_response(
            {"ok": False, "error": f"unknown event type: {data.get('type')}"},
            status=400,
        )

    if changed:
        await broadcast({"type": "state_update", "state": get_full_state()})

    return web.json_response({"ok": True, "changed": changed})


async def route_state(req: web.Request) -> web.Response:
    return web.json_response(get_full_state())


async def route_ws(req: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    ws_clients.add(ws)

    try:
        # Immediately push current state to new subscriber
        await ws.send_json({"type": "state_update", "state": get_full_state()})

        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        ws_clients.discard(ws)

    return ws


async def route_dashboard(req: web.Request) -> web.Response:
    return _serve_html(DASHBOARD_PATH, "dashboard.html")


async def route_tasks(req: web.Request) -> web.Response:
    return _serve_html(TASKS_PATH, "tasks.html")


def _serve_html(path: Path, name: str) -> web.Response:
    if path.exists():
        html = path.read_text(encoding="utf-8")
    else:
        html = (
            f"<h2 style='font-family:sans-serif;padding:40px'>"
            f"{name} not found alongside daemon.py</h2>"
        )
    return web.Response(text=html, content_type="text/html", charset="utf-8")


# ─── Broadcast ────────────────────────────────────────────────────────────────

async def broadcast(msg: dict):
    global ws_clients
    if not ws_clients:
        return
    text = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in list(ws_clients):
        try:
            await ws.send_str(text)
        except Exception:
            dead.add(ws)
    ws_clients -= dead


# ─── 用量窗口（5h / 7d）扫描 ──────────────────────────────────────────────────

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
USAGE_WIN_5H = timedelta(hours=5)
USAGE_WIN_7D = timedelta(days=7)
USAGE_SCAN_INTERVAL = 60.0  # 秒

# 文件级缓存：mtime 没变就跳过整文件解析
_usage_file_mtime: dict[str, float] = {}
_usage_file_entries: dict[str, list] = {}   # path -> [(ts: datetime, tokens: int, is_user: bool)]
_usage_window = {
    "h5_tokens": 0, "h5_prompts": 0,
    "d7_tokens": 0, "d7_prompts": 0,
    "computed_at": None,
}


def _parse_iso(ts_str: str):
    """容错 ISO 8601：把 Z 当 UTC；无时区一律按 UTC 处理。"""
    if not ts_str:
        return None
    s = ts_str
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_transcript_file(path: Path) -> list:
    """解析单个 .jsonl，返回 [(timestamp, total_tokens, is_user_prompt)]。"""
    out: list = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts = _parse_iso(e.get("timestamp") or "")
                if ts is None:
                    continue
                etype = e.get("type")
                if etype == "user":
                    out.append((ts, 0, True))
                elif etype == "assistant":
                    u = (e.get("message") or {}).get("usage") or {}
                    tok = (int(u.get("input_tokens") or 0)
                           + int(u.get("output_tokens") or 0)
                           + int(u.get("cache_read_input_tokens") or 0)
                           + int(u.get("cache_creation_input_tokens") or 0))
                    if tok > 0:
                        out.append((ts, tok, False))
    except OSError:
        pass
    return out


def scan_usage_windows() -> bool:
    """返回值表示统计结果是否相比上次有变化（用于决定要不要广播）。"""
    if not CLAUDE_PROJECTS_DIR.exists():
        return False

    # 先把目录里所有 jsonl 找出来；并 prune 已被删除的文件
    seen_paths: set[str] = set()
    for jp in CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
        sp = str(jp)
        seen_paths.add(sp)
        try:
            mt = jp.stat().st_mtime
        except OSError:
            continue
        if _usage_file_mtime.get(sp) == mt:
            continue
        _usage_file_mtime[sp] = mt
        _usage_file_entries[sp] = _parse_transcript_file(jp)

    # 清理已不存在的文件缓存
    for stale in list(_usage_file_mtime.keys()):
        if stale not in seen_paths:
            _usage_file_mtime.pop(stale, None)
            _usage_file_entries.pop(stale, None)

    # 汇总
    now_utc = datetime.now(timezone.utc)
    cut5h = now_utc - USAGE_WIN_5H
    cut7d = now_utc - USAGE_WIN_7D
    h5_tok = h5_prm = d7_tok = d7_prm = 0
    for entries in _usage_file_entries.values():
        for ts, tok, is_user in entries:
            if ts < cut7d:
                continue
            if is_user:
                d7_prm += 1
                if ts >= cut5h:
                    h5_prm += 1
            else:
                d7_tok += tok
                if ts >= cut5h:
                    h5_tok += tok

    new_snapshot = {"h5_tokens": h5_tok, "h5_prompts": h5_prm,
                    "d7_tokens": d7_tok, "d7_prompts": d7_prm}
    old_snapshot = {k: _usage_window[k] for k in new_snapshot}
    changed = new_snapshot != old_snapshot

    _usage_window.update(new_snapshot)
    _usage_window["computed_at"] = now_utc.astimezone().isoformat(timespec="seconds")
    return changed


# ─── Anthropic OAuth /api/oauth/usage 真实配额 ────────────────────────────────
# 从 ~/.claude/.credentials.json 读 access token，调用 Claude Code 内部用的同一端点。
# 返回 five_hour / seven_day / seven_day_sonnet / seven_day_opus 的 utilization %。
# **非官方公开 API**，Claude Code 升级可能改协议——失败时回退到本地估算。

CRED_PATH = Path.home() / ".claude" / ".credentials.json"
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_POLL_INTERVAL = 1800.0   # 30 分钟（手动刷新按钮兜底）

_oauth_usage = {
    "data": None,           # 原始 JSON
    "fetched_at": None,     # ISO 时间
    "error": None,          # 上次失败原因（成功时清空）
    "subscription_type": None,
}


def _load_oauth_token() -> str | None:
    try:
        d = json.loads(CRED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = d.get("claudeAiOauth") or {}
    if oauth.get("subscriptionType"):
        _oauth_usage["subscription_type"] = oauth["subscriptionType"]
    return oauth.get("accessToken") or None


def _sync_fetch_oauth_usage():
    """同步版本，stdlib urllib（aiohttp 被 Cloudflare 按 TLS 指纹拦截 403）。
    返回 (ok, data, err_msg)。"""
    token = _load_oauth_token()
    if not token:
        return False, None, "no oauth token in ~/.claude/.credentials.json"
    req = urllib.request.Request(
        OAUTH_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read()[:200].decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, None, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


async def fetch_oauth_usage() -> bool:
    """成功返回 True，并把结果写入 _oauth_usage。"""
    loop = asyncio.get_event_loop()
    ok, data, err = await loop.run_in_executor(None, _sync_fetch_oauth_usage)
    if ok:
        _oauth_usage["data"]       = data
        _oauth_usage["fetched_at"] = now()
        _oauth_usage["error"]      = None
    else:
        _oauth_usage["error"] = err
    return ok


async def oauth_usage_loop(app: web.Application):
    """启动时拉一次，之后完全手动（点 ↻ 按钮触发 usage_refresh）。
    不做周期轮询——避免高频调用非官方端点引发风控。"""
    try:
        if await fetch_oauth_usage():
            await broadcast({"type": "state_update", "state": get_full_state()})
    except Exception:
        pass


async def usage_scan_loop(app: web.Application):
    # 启动后立刻先扫一遍，避免页面打开时全 0
    try:
        scan_usage_windows()
    except Exception:
        pass
    while True:
        try:
            await asyncio.sleep(USAGE_SCAN_INTERVAL)
            if scan_usage_windows():
                await broadcast({"type": "state_update", "state": get_full_state()})
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(USAGE_SCAN_INTERVAL)


# ─── 后台轮询：TASKS.md 文件变更 ─────────────────────────────────────────────

PLAN_POLL_INTERVAL = 3.0  # 秒

async def plan_poll_loop(app: web.Application):
    while True:
        try:
            await asyncio.sleep(PLAN_POLL_INTERVAL)
            changed_any = False
            for proj in known_projects():
                try:
                    if scan_project_plan(proj):
                        changed_any = True
                except Exception:
                    pass
            if changed_any:
                await broadcast({"type": "state_update", "state": get_full_state()})
        except asyncio.CancelledError:
            break
        except Exception:
            # 任何意外都不要让轮询挂掉
            await asyncio.sleep(PLAN_POLL_INTERVAL)


async def _start_bg_tasks(app: web.Application):
    app["plan_poll"]   = asyncio.create_task(plan_poll_loop(app))
    app["usage_scan"]  = asyncio.create_task(usage_scan_loop(app))
    app["oauth_usage"] = asyncio.create_task(oauth_usage_loop(app))


async def _stop_bg_tasks(app: web.Application):
    for key in ("plan_poll", "usage_scan", "oauth_usage"):
        task = app.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ─── App Factory ──────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            })
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    app = web.Application(middlewares=[cors_middleware])

    app.router.add_get("/", route_dashboard)
    app.router.add_get("/tasks", route_tasks)
    app.router.add_get("/ws", route_ws)
    app.router.add_post("/api/event", route_event)
    app.router.add_get("/api/state", route_state)

    app.on_startup.append(_start_bg_tasks)
    app.on_cleanup.append(_stop_bg_tasks)

    return app


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    banner = (
        "\n  Claude Board\n"
        f"    Dashboard  ->  http://localhost:{PORT}\n"
        "    Events     ->  POST /api/event\n"
        "    State      ->  GET  /api/state\n"
        f"    DB         ->  {DB_PATH}\n"
    )
    try:
        print(banner)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(banner.encode("utf-8", errors="replace"))
    app = create_app()
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)
