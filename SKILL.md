---
name: claude-board
description: >
  Real-time web dashboard agent for tracking multiple Claude Code sessions. Maintains
  a live kanban board showing tasks (todo/doing/done with % progress) and full
  conversation history (collapsible rounds) for every active terminal. Trigger this
  skill whenever a Claude Code session starts, when the user describes a plan or
  task list, when tasks are completed, or when the user asks about session history,
  progress, or wants to resume a previous conversation. Also trigger when the user
  says "show the board", "track this", "what's my progress", or similar.
compatibility:
  - requires: claude-board daemon running at localhost:7820
  - setup: python3 ~/.claude-board/setup.py
---

# Claude Board — Skill Instructions

This skill keeps the Claude Board web dashboard (http://localhost:7820) synchronized
with everything happening in this Claude Code session.

## Daemon URL

All events POST to: `http://localhost:7820/api/event`

Use Python (stdlib only — no extra deps needed from hooks):

```python
import json, urllib.request, os

def board(event: dict):
    try:
        req = urllib.request.Request(
            "http://localhost:7820/api/event",
            data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # silently skip if daemon not running

SESSION_ID = os.environ.get("CLAUDE_SESSION_ID", "unknown")
```

---

## 1. Session Registration — do at conversation start

```python
board({
    "type":       "session_init",
    "session_id": SESSION_ID,
    "title":      "4-8 word description of what user wants to do",
    "project":    os.getcwd(),
})
```

**Title rules:**

- Derive from the user's first message (verb + object format)
- Examples: "Refactor auth pipeline", "Build transcript editor", "Fix WebCodecs decoder"
- Update later with `session_title` if first message was ambiguous

```python
# Update title once intent is clear
board({"type": "session_title", "session_id": SESSION_ID, "title": "Better title here"})
```

---

## 2. Conversation Rounds — post after each exchange

Post the user's message **before** you reply:

```python
board({
    "type":       "message",
    "session_id": SESSION_ID,
    "role":       "user",
    "content":    "<user's message, up to 2000 chars>",
})
```

Post your summary **after** you finish responding (keep it concise — key decisions/results):

```python
board({
    "type":       "message",
    "session_id": SESSION_ID,
    "role":       "assistant",
    "content":    "<2-4 sentence summary of what you did/decided/produced>",
})
```

**Content length guidance:**

- User messages: first 2000 chars is fine; truncate very long pastes
- Assistant summaries: 1-4 sentences; outcomes-focused, not step-by-step

---

## 3. Task Sync — after every TodoWrite

The `PostToolUse` hook handles this automatically if configured.
As a manual fallback (or to be explicit), after every `TodoWrite` call:

```python
board({
    "type":       "task_sync",
    "session_id": SESSION_ID,
    "tasks": [
        {"id": "t1", "content": "Design the API",           "status": "completed", "priority": "high"},
        {"id": "t2", "content": "Implement chunk streaming", "status": "in_progress", "priority": "high"},
        {"id": "t3", "content": "Write unit tests",         "status": "pending",   "priority": "medium"},
    ]
})
```

Status values: `"pending"` | `"in_progress"` | `"completed"`
Priority values: `"high"` | `"medium"` | `"low"`

**Important:** Send the FULL task list every time — the daemon does a full replace.
This matches how `TodoWrite` works (full state, not deltas).

---

## 4. When to call what

| Trigger                   | Call                              |
| ------------------------- | --------------------------------- |
| Conversation starts       | `session_init`                    |
| User sends a message      | `message` (role: user)            |
| You finish your response  | `message` (role: assistant)       |
| After any `TodoWrite`     | `task_sync` (hook does this auto) |
| User asks to update title | `session_title`                   |

---

## 5. Hooks configuration (automatic task sync)

The setup script writes this to `.claude/settings.local.json` automatically.
If needed, add manually:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "TodoWrite",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude-board/hooks/post_tool_use.py"
        }]
      }
    ],
    "Stop": [
      {
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude-board/hooks/stop.py"
        }]
      }
    ]
  }
}
```

---

## 6. Checking daemon status

If the daemon might not be running, check first:

```bash
curl -s http://localhost:7820/api/state | python3 -m json.tool | head -5
# If no response: start the daemon in a separate terminal:
#   python3 ~/.claude-board/daemon.py
```

---

## 7. Behavior contract

- **Always** call `session_init` within your first tool call
- **Always** post `message` events for user + assistant turns (the board's conversation
  history depends entirely on this — hooks don't capture raw conversation text)
- **Never** block or retry if the daemon is down — use `timeout=2` and `except: pass`
- Task sync via hooks is preferred; manual `task_sync` calls are the fallback
- Keep assistant message content outcome-focused (not narration of what you did)

---

## 8. Working with TASKS.md  ⭐ persistent project plan

If the project root contains a file named **`TASKS.md`**, that file is the
**persistent, cross-session source of truth** for the project's plan. The board
parses it automatically (3-second polling + Edit/Write hook trigger), shows it
under "📋 项目规划", and links it to the active session as a parent-task indicator.

### Format the board understands

Only GitHub-style checkbox lines are parsed; everything else is ignored:

```markdown
## Phase 1                  ← `##` becomes a group label
- [ ] Pending task          ← unchecked = pending
- [x] Done task             ← x = completed
- [ ] 🔄 In-progress task   ← inline 🔄 marks "a session is working on this"
  - [ ] Nested sub-task     ← indent (2 spaces / 1 tab) = parent/child
```

### Your responsibilities when a TASKS.md exists

1. **Read it at session start.** It defines what the user actually wants done.
   Your TodoWrite/TaskCreate items should serve a TASKS.md item, not invent
   parallel work.

2. **Mark what you're starting.** When you begin work on a TASKS.md item,
   use `Edit` to add `🔄` after the checkbox on that line. Do this *before*
   creating in-session tasks so the dashboard's "正在做" bar lights up.
   
   ```
   - [ ] 重构图片上传模块        →     - [ ] 🔄 重构图片上传模块
   ```

3. **Mark what you finish.** When the item is fully done, use `Edit` to flip
   `[ ]` → `[x]` **and remove the 🔄**.
   
   ```
   - [ ] 🔄 重构图片上传模块     →     - [x] 重构图片上传模块
   ```

4. **Only Claude edits TASKS.md.** The daemon will never auto-write to this
   file. Every state change must be a visible `Edit` call so the user can
   see it in chat and review.

5. **One 🔄 at a time per project.** If multiple sessions are running in the
   same project, only the one actively working on an item should mark it.
   Clear stale 🔄 before adding your own.

### What if TASKS.md doesn't exist?

Don't auto-create it. Just plan in-session via TaskCreate/TodoWrite as usual.
TASKS.md is opt-in per project; suggest creating one only when the user
mentions "ongoing plan", "track across sessions", or similar.
