# claude-board 项目规划

> 格式说明见 [README.md](./README.md)、协作约定见 [SKILL.md](./SKILL.md) 第 8 节。
> 本文件只放实际待办，daemon 会自动同步到看板。

## 阶段 1：双向同步基础设施

- [x] 设计 TASKS.md 格式规范
- [x] daemon: 新增 project_plans 表 + markdown 解析器
- [x] daemon: TASKS.md 文件轮询 + 事件接入
- [x] daemon: get_full_state 输出 project_plans

## 阶段 2：前端展示

- [x] dashboard.html: 卡片显示「正在做」横条
- [x] tasks.html: 项目规划与 session 任务合并展示
- [x] dashboard.html / tasks.html markdown 渲染

## 阶段 3：协作约定

- [x] SKILL.md: 加 TASKS.md 协作约定

## 后续可选

- [ ] hooks/post_tool_use.py 兼容更多任务工具（已覆盖 TodoWrite / TaskCreate / TaskUpdate）
- [ ] 提供 `--reset` 一键清理 board.db
- [ ] 支持多 TASKS.md 文件（按子目录拆分大型项目）
