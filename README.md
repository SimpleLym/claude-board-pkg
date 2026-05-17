# Claude 任务看板（claude-board）

一个本地、实时的 Web 看板，把同一台机器上所有 Claude Code 会话的**任务规划**、**对话历史**、**项目级 TASKS.md 计划**集中到一个浏览器页面里。

不再为"我之前那个会话规划了啥来着？" 翻几个终端、也不再担心跨会话的长线任务被遗忘。

![架构](https://img.shields.io/badge/-本地运行-22c55e?style=flat) ![Python](https://img.shields.io/badge/Python-3.10+-blue) ![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## 解决什么问题

平时同时开 3~5 个 Claude Code 终端、并行做不同项目的事情很常见。痛点：

- **看不到全貌**：不知道每个会话在做什么、规划到哪一步
- **跨会话健忘**：今天起的活儿、明天另开一个终端就忘了
- **规划散落**：有时候是 Claude `TodoWrite` 的临时拆解，有时候是写在文档里的长期计划，两边对不上

claude-board 把这些信息集中起来：

| 数据源                              | 体现在看板上                                   |
| -------------------------------- | ---------------------------------------- |
| 每个 Claude Code 会话的对话历史           | 卡片里的"我 / ◈"消息流，支持 markdown 渲染            |
| `TodoWrite` / `TaskCreate` 的临时规划 | 卡片右下角紫色"规划 N/M"徽章 + 点击展开侧栏               |
| 项目根目录的 `TASKS.md`（持久规划）          | 与会话任务**并入同一个 3 列看板**，蓝色"📋 TASKS.md"徽章标识 |
| 上一天没做完的任务                        | 主看板顶部黄色"昨日未完成"折叠横幅，跨天自动展开                |

---

## 截图速览

- 主看板（`http://localhost:7820/`）：每个 Claude Code 会话一张卡，含会话时间线、进度条、悬浮规划面板
- 全部任务汇总（`http://localhost:7820/tasks`）：3 列看板，session 任务 + TASKS.md 项目计划合并展示
- 消息全文窗：点卡片内任意一条消息 → 弹出 markdown 渲染的全文，可一键切换原文/复制

---

## 安装

### 1. 依赖

只有一个：

```bash
pip install -r requirements.txt
# 实际只装 aiohttp
```

要求 Python ≥ 3.10。

### 2. 一次性 setup

把 daemon + hooks 复制到 `~/.claude-board/`，并自动写入 Claude Code 全局 hooks 配置（`~/.claude/settings.json`）：

```powershell
python E:\path\to\claude-board-pkg\setup.py
```

加 `--project` 则只对当前仓库生效（写入 `.claude/settings.local.json`）：

```powershell
python E:\path\to\claude-board-pkg\setup.py --project
```

这一步会注册四个 hook：

| Hook               | 触发时机                                               | 作用                         |
| ------------------ | -------------------------------------------------- | -------------------------- |
| `SessionStart`     | 新会话开始                                              | 注册到 daemon，扫描 TASKS.md     |
| `UserPromptSubmit` | 用户每发一条消息                                           | 推送对话 + 自动取标题               |
| `PostToolUse`      | TodoWrite / TaskCreate / TaskUpdate / Edit / Write | 同步任务状态；编辑 TASKS.md 时秒级触发重扫 |
| `Stop`             | Claude 回复完成                                        | 抓取最近回复推送到看板                |

> 改了源码后**重跑一次 setup.py** 把新文件同步到 `~/.claude-board/`，老的 hook 配置会被合并保留。

---

## 启动

新开一个终端，**保持它一直运行**：

```powershell
python "$env:USERPROFILE\.claude-board\daemon.py"
```

或开发调试直接跑项目里的版本：

```powershell
python E:\path\to\claude-board-pkg\daemon.py
```

看到这个 banner 就 OK：

```
  Claude Board
    Dashboard  ->  http://localhost:7820
    Events     ->  POST /api/event
    State      ->  GET  /api/state
    DB         ->  C:\Users\xxx\.claude-board\board.db
```

浏览器打开 **<http://localhost:7820/>**。之后任何启动的 Claude Code 会话都会自动上报。

### 端口冲突

```powershell
$env:CLAUDE_BOARD_PORT="9000"; python E:\path\to\claude-board-pkg\daemon.py
```

---

## 核心功能

### 1. 主看板（`/`）

- **卡片网格**：每个会话一张，浅色科技风
- **会话时间线**：卡片里展示最近 8 条对话；点任意一条 → 弹出全文窗，markdown 渲染
- **"正在做"横条**：卡片顶部紫色横条显示**该 session 当前 in_progress 的任务**（不再混淆项目级 🔄）
- **悬浮规划面板**：点紫色"规划 N/M"徽章 → 右侧滑出，三段任务列表（进行中 / 待办 / 已完成）；可收起成竖条
- **昨日未完成横幅**：顶部黄色折叠条，列出今天之前所有 pending/in_progress 任务，按项目分组；点击任务跳转到对应规划面板；跨天首次打开自动展开
- **进行中 / 已结束 切换**：toolbar 切换；会话发 `session_stop` 后归入"已结束"
- **实时刷新**：基于 WebSocket，签名差异渲染，**无闪烁**

### 2. 全部任务汇总（`/tasks`）

- 三列看板：**进行中 / 待办 / 已完成**
- **session 任务（紫色徽章）和 TASKS.md 项目计划（蓝色徽章）合并展示**
- 分组：按项目 / 按会话 / 不分组
- "隐藏已完成"开关
- 任意 session 徽章点击复制 `claude --resume <id>`；TASKS.md 徽章点击复制文件路径

### 3. TASKS.md 项目级持久计划 ⭐

约定项目根目录的 `TASKS.md` 是该项目跨会话的长期规划：

```markdown
## 阶段 1：基础设施
- [ ] 设计 API 结构
- [ ] 🔄 实现核心模块         ← 行内 🔄 表示有会话正在做
- [x] 写单元测试              ← x = 已完成
  - [x] 子任务：mock 数据    ← 缩进 = 父子
```

**daemon 怎么同步**：

- 3 秒一轮文件 mtime 轮询，变了就重解析
- 配合 PostToolUse hook：Claude 用 Edit 改 TASKS.md 时**立刻触发** `plan_scan`，秒级生效
- 解析器跳过 ` ``` ` 围栏的代码块（避免示例被当真任务）

**协作约定**（写在 `SKILL.md` 第 8 节）：

| 时机             | 操作                                 |
| -------------- | ---------------------------------- |
| 开始做某项          | 用 Edit 在 `[ ]` 后面加 `🔄`            |
| 完成             | 用 Edit 把 `[ ]` 改成 `[x]` 并移除 `🔄`   |
| daemon 是否自动写文件 | **否**——只能 Claude 自己改，保证每次写入都在对话里可见 |

---

## 目录结构

```
claude-board-pkg/
├── daemon.py              HTTP + WebSocket 服务（aiohttp）
├── dashboard.html         主看板单页应用
├── tasks.html             /tasks 汇总页
├── setup.py               一次性安装：拷贝文件 + 注册 hooks
├── requirements.txt       aiohttp
├── SKILL.md               Claude Code Skill 定义（含 TASKS.md 协作约定）
├── TASKS.md               本项目自己的规划，同时是格式示例
├── README.md              本文件
└── hooks/
    ├── session_start.py        SessionStart hook
    ├── user_prompt_submit.py   UserPromptSubmit hook
    ├── post_tool_use.py        PostToolUse hook (TodoWrite/TaskCreate/Edit 等)
    └── stop.py                 Stop hook
```

数据落盘 → `~/.claude-board/board.db`（SQLite + WAL）。

---

## HTTP / WebSocket 接口

| 路径           | 方法   | 说明                  |
| ------------ | ---- | ------------------- |
| `/`          | GET  | 主看板 HTML            |
| `/tasks`     | GET  | 全部任务汇总 HTML         |
| `/api/state` | GET  | 当前完整 state 快照（JSON） |
| `/api/event` | POST | hooks/外部工具上报事件      |
| `/ws`        | WS   | 看板订阅实时推送            |

**事件类型**（POST `/api/event`）：

- `session_init` —— 注册/更新会话信息
- `session_title` / `session_title_default` —— 设置标题
- `session_stop` —— 标记会话结束
- `message` —— 推送一条对话（role: user/assistant）
- `task_sync` —— 整体替换该 session 的任务列表（TodoWrite）
- `task_upsert` —— 单条任务新增/更新（TaskCreate/TaskUpdate）
- `plan_scan` —— 强制重扫某项目的 TASKS.md

---

## 升级 / 重置

**改了源码**：

```powershell
python E:\path\to\claude-board-pkg\setup.py    # 同步新文件
# Ctrl+C 旧 daemon，重新启动
python E:\path\to\claude-board-pkg\daemon.py
```

**清空数据库**（不影响代码和 hook 配置）：

```powershell
Remove-Item "$env:USERPROFILE\.claude-board\board.db*"
```

**撤销 hooks**：手动从 `~/.claude/settings.json` 删除 `hooks.SessionStart/UserPromptSubmit/PostToolUse/Stop` 中以 `~/.claude-board/hooks/` 开头的条目。

---

## 已知限制

- Windows 上 stdin 默认 cp936，hooks 显式按 UTF-8 解码（已修复）。Linux/macOS 不受影响。
- `settings.json` 的 hook matcher 修改**不会**在当前 Claude Code 会话热加载——升级 setup.py 后新匹配规则只对**之后启动的** Claude Code 终端生效。
- `TaskCreate` 的 `tool_response` 字段名未跨版本验证，hook 用了内容 md5 兜底 task_id，可能导致后续 `TaskUpdate` 找不到对象 → 显示为两条独立任务。真实跑起来观察到再调。

---

## License

MIT
