# CLAUDE.md

本文件为 Claude Code 在此仓库工作时的导航手册。

---

## 语言与沟通规范

- **全程使用简体中文**：所有回复、分析、终端反馈均须以简体中文输出。
- **语调**：资深工程师风格——专业、精简、直击要害，不废话。

---

## 常用命令

```bash
# 初始化并激活虚拟环境
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
.venv/bin/playwright install chromium   # 首次安装后执行一次

# 启动各服务（本地开发）
.venv/bin/python -m stock_bot.tg_main   # OmniStock Telegram Bot
.venv/bin/python -m ehs_bot.tg_main     # OmniEHS Telegram Bot
.venv/bin/python -m stock_bot.daily_job          # 盘后调度器（等待 15:30）
.venv/bin/python -m stock_bot.daily_job --test   # 立即执行一次（测试模式）

# Docker - Bot 服务
docker build -f Dockerfile.base -t omnibot-base:latest .   # 首次 / 依赖变更
docker compose up -d --build                                # 启动所有 bot 服务
docker compose logs -f                                      # 查看全量日志
docker compose logs ehs-tg-bot --tail=50                    # 查看单个服务日志
docker compose restart stock-tg-bot                         # 重启单个服务

# Docker - obs 可观测性平台（独立 compose，在 obs/ 目录下操作）
docker compose -f obs/docker-compose.yml up -d --build     # 启动 obs
docker compose -f obs/docker-compose.yml logs api -f        # 查看 API 日志
docker compose -f obs/docker-compose.yml restart api        # 重启 API

# 手动触发摄取
curl -X POST http://localhost:8000/api/ingest/mhxy
curl -X POST http://localhost:8000/api/ingest/stock-bot
curl -X POST http://localhost:8000/api/ingest/ehs-bot

# 运行单元测试（在容器内跑，不依赖本地 venv）
# core/ 和 mhxy_bot/ 已通过 volume 挂载进容器，测试文件需先 cp 进去
docker exec v2-omnimhxy-tg-bot mkdir -p /app/tests
docker cp tests/<file>.py v2-omnimhxy-tg-bot:/app/tests/<file>.py
docker exec v2-omnimhxy-tg-bot python -m pytest tests/<file>.py -v
# 批量跑所有测试（先把整个 tests/ 目录 cp 进容器）：
# docker cp tests/ v2-omnimhxy-tg-bot:/app/tests
# docker exec v2-omnimhxy-tg-bot python -m pytest tests/ -v
```

---

## Windows Executor 运维

> 详细运维手册（重启流程、已知坑、adb 路径）：[ops/windows_executor.md](ops/windows_executor.md)

MHXY Windows executor 默认运行在 Windows 主机 `192.168.100.149`，HTTP 服务地址：

```text
http://192.168.100.149:8765
```

Windows 侧工作目录：

```text
C:\Users\sdw\mhxy_executor
```

从当前宿主机连入 Windows executor：

```bash
ssh -i /home/shudawei/.ssh/id_towin -o StrictHostKeyChecking=no sdw@192.168.100.149
```

通过 SSH 执行 Windows PowerShell 命令：

```bash
ssh -i /home/shudawei/.ssh/id_towin -o StrictHostKeyChecking=no sdw@192.168.100.149 "powershell -NoProfile -Command \"<PowerShell 命令>\""
```

同步文件到 Windows executor：

```bash
scp -i /home/shudawei/.ssh/id_towin -o StrictHostKeyChecking=no <本地文件> sdw@192.168.100.149:C:/Users/sdw/mhxy_executor/<目标文件>
```

健康检查：

```bash
curl http://192.168.100.149:8765/health
```

相关默认配置位置：
- `mhxy_bot/executor/watchdog.py`：`EXECUTOR_URL`、`REMOTE_HOST`、`WINDOWS_EXECUTOR_DIR`、容器内 `SSH_KEY`
- `mhxy_bot/runner/task_loader.py`：runner 默认 `MHXY_EXECUTOR_URL`
- `entrypoint.mhxy.sh`：容器内 SSH key 从 `/root/.ssh_host/id_towin` 复制到 `/root/.ssh_runtime/id_towin`

注意：这里只记录 key 路径和命令模板，不能把私钥内容写入仓库。

---

## 环境变量（`.env`）

```env
# 必填
MINIMAX_API_KEY=                   # 主模型推理（MiniMax-M2.7 via MiniMax）
DASHSCOPE_APIMODE_KEY=    # RAG 向量化（text-embedding-v3）

# Stock Bot（stock_bot/tg_main.py 启动时必填）
TG_BOT_TOKEN=
ALLOWED_TG_USERS=           # 白名单用户 ID，逗号分隔

# EHS Bot（ehs_bot/tg_main.py 启动时必填）
EHS_TG_BOT_TOKEN=
EHS_ALLOWED_TG_USERS=

# 可选（邮件推送）
SMTP_SERVER=
SMTP_PORT=465
SENDER_EMAIL=
SENDER_PASSWORD=
RECEIVER_EMAIL=
```

- `.env` 通过 Docker volume 挂载进容器，**不得打入镜像**（`.dockerignore` 已排除）。
- `DASHSCOPE_CODINGPLAN_KEY` / `DASHSCOPE_APIMODE_KEY`：各 bot 的 `agent.py` 启动时立即校验，缺失直接 `ValueError` 终止。

---

## 代码规范（强制执行）

### Python 工程化
- **类型提示**：所有核心函数与类必须使用完整 Type Hints（PEP 484）。
- **Docstring**：核心函数和类必须包含 Google 风格 Docstring，包括 `Args`、`Returns`、`Raises`。
- **异常捕获**：必须捕获具体的 Exception 子类，**严禁裸露的 `except:`**。
- **依赖原则**：优先使用标准库；引入新第三方库前必须先询问用户，再修改 `requirements.txt`。

### 本项目专属业务规范
- **网络容错**：所有调用外部 API 的工具函数（yfinance、akshare、DashScope、DDGS、SMTP）必须配置 `tenacity` 指数退避重试（3 次，wait 2–10s）。
- **数据解析防御**：解析外部数据或 JSON 时，必须做空值检查（`if df is None or df.empty`）和 `json.JSONDecodeError` 捕获，绝不假设数据完整。
- **沙箱路径校验**：文件路径限制统一使用 `pathlib.is_relative_to()`，**严禁改用 `str.startswith()`**（无法防御同级目录绕过）。

---

## 工作流红线（必须遵守）

### 🛑 Git 操作强制确认
**严禁未经允许擅自执行代码提交。** 执行 `git commit`、`git push` 或任何不可逆变更前，**必须在对话中明确询问用户**并获得授权后方可继续。

### 📝 任务完成后的修改总结
每次完成代码修改后，**必须主动输出简明总结**，包含：
1. 改动了哪些核心文件；
2. 新增或变更了哪些关键逻辑；
3. 下一步建议或潜在风险提示。

### 🐍 虚拟环境强制约束
- 执行任何 Python 脚本或安装依赖前，必须确保使用项目虚拟环境。
- 推荐格式：`./.venv/bin/python ...` 或 `source .venv/bin/activate && python ...`

---

## 系统架构

### Monorepo 结构

```
omnibot/
├── core/                    # 共享基础层
│   ├── tg_base.py           # TelegramBotBase + 渲染管道 + broadcast_to_telegram
│   ├── agent_base.py        # build_agent() 工厂
│   ├── notifier.py          # SMTP 邮件推送
│   ├── job_runner.py        # 通用独立子进程启动器
│   └── tools/
│       ├── file_tools.py    # 沙箱 I/O + L1/L2 RAG 缓存引擎
│       ├── memory_tools.py  # LTM KV 状态机 + STM 滑动窗口
│       └── job_tools.py     # trigger_job / query_job_status
│
├── stock_bot/               # OmniStock 量化助理
│   ├── agent.py             # 目录配置 + System Prompt + 工具组装
│   ├── tg_main.py           # StockBot(TelegramBotBase) 子类
│   ├── daily_job.py         # 盘后调度器（每日 15:30）
│   ├── valuation_engine.py  # 多币种估值引擎（独立模块，禁止 LLM 绕过）
│   └── tools/stock_tools.py # make_stock_tools() 工厂
│
├── ehs_bot/                 # OmniEHS 安全合规助理
│   ├── agent.py             # 目录配置 + System Prompt + 工具组装
│   ├── tg_main.py           # EHSBot(TelegramBotBase) 子类
│   ├── daily_job.py         # EHS 定期简报调度器
│   └── tools/ehs_tools.py   # make_ehs_tools() 工厂
│
├── data/
│   ├── stock/{memory,knowledge_base,embeddings,agent_workspace}/
│   └── ehs/{memory,knowledge_base,embeddings,agent_workspace}/
│
├── jobs/{status,logs}/      # 研报子进程任务状态与日志
│
└── obs/                     # 可观测性平台（独立 Docker Compose）
    ├── api/                 # FastAPI + SQLAlchemy + PostgreSQL
    ├── web/                 # Next.js 统计看板（端口 3100）
    └── docker-compose.yml   # 三容器：obs-api-1 / obs-web-1 / obs-db-1
```

### 模块职责

| 文件 | 职责 |
|------|------|
| `core/tg_base.py` | Bot 基础设施：鉴权、渲染管道（HTML/表格图）、Agent 异步调度、文件上传入库、跨进程广播接收；`obs_provider` 参数可为每个 bot 指定 LLM provider |
| `core/agent_base.py` | 统一构建带记忆的 LangChain Agent（`RunnableWithMessageHistory`） |
| `core/observability.py` | JSONL 会话日志器（`OmniObserver`）+ LangChain 回调（`OmnibotObsCallbackHandler`）；写入 `{obs_dir}/{session_id}.jsonl`；session_id 格式：`tg_session_{agent_slug}_{user_id}_{YYYYMMDD}` |
| `core/tools/file_tools.py` | 沙箱读写 + RAG（L1 `lru_cache` / L2 FAISS 硬盘，mtime 热更新） |
| `core/tools/memory_tools.py` | LTM：`user_profile.json` KV 状态机；STM：10 条滑动窗口 `FileChatMessageHistory` |
| `core/tools/job_tools.py` | `trigger_job` 以 `subprocess.Popen` 启动 `core.job_runner`；`query_job_status` 读取状态文件 |
| `core/job_runner.py` | 独立子进程启动器：`importlib` 动态加载任务模块，写入 `jobs/status/{job_id}.json` |
| `stock_bot/valuation_engine.py` | 唯一财务计算来源：Ticker 格式化中间件、多源查价（yfinance/akshare）、K 线图、多币种 CNY 折算 |
| `stock_bot/daily_job.py` | 多源资讯聚合去重（财联社/新浪/东财）、LLM 报告生成、邮件推送、知识库归档、Telegram 广播 |
| `ehs_bot/tools/ehs_tools.py` | EHS 专属：法规联网搜索、GHS 化学品查询、隐患日志（JSONL）、作业许可证检查清单（内置） |

### 关键设计模式

**TelegramBotBase 钩子体系**：子类只需实现以下钩子方法即可获得完整 Bot 能力：
- `get_bot_name()` / `get_bot_commands()` / `get_dashboard_keyboard()` / `get_welcome_text()`
- `get_tool_status_map()` — 工具调用状态文案（追加到默认映射）
- `setup_extra_handlers(app)` — 注册额外命令处理器（`_post_init` 异步钩子中调用）
- `setup_job_queue(app)` — 注册定时任务（如价格预警轮询）
- `handle_custom_cmd(cmd, query, user_id, context, update)` — Inline Keyboard 按钮分发

**Observability 接入方式**：`TelegramBotBase.__init__` 传入 `obs_dir: Path` 和 `agent_id: str`，框架自动在每次 `execute_agent_task` 中创建 `OmniObserver` 并挂载 `OmnibotObsCallbackHandler`。`obs_provider` 参数（默认 `"dashscope"`）透传给 handler，记录到 JSONL 的 `provider` 字段。JSONL 文件由 `obs/` 模块周期性摄取入 PostgreSQL（`obs/api/app/adapters/omnibot_jsonl.py` 与此文件存在字段契约，改字段名须同步更新 adapter）。

**`broadcast_to_telegram` 为模块级独立函数**（非类方法）：daily_job 子进程需要调用它，不能依赖类实例。签名为 `broadcast_to_telegram(text, bot_token, allowed_user_ids, sandbox_dir)`。

**工具工厂函数模式**：所有工具通过工厂函数（`make_*_tools(dir1, dir2, ...)`）绑定运行时目录，返回 LangChain `@tool` 列表。调用方在 `agent.py` 中合并后传入 `build_agent()`。

**Ticker 格式化中间件**（`format_universal_ticker` in `valuation_engine.py`）：LLM 只传入用户原始输入（如 `"0700"`），中间件自动补齐 `.HK`、`.SS`、`.SZ` 市场后缀。调用 yfinance/akshare 时**绝对不能绕过此函数**。

**L1/L2 混合 RAG 缓存**：`analyze_local_document` 按以下顺序命中：进程内 `lru_cache`（L1）→ `embeddings/<file>_vstore/`（L2，附 `meta.json` mtime 校验）→ 均未命中则重建并双向写入。

**研报独立进程架构**：`trigger_job` 工具以 `subprocess.Popen(..., start_new_session=True)` 启动 `core.job_runner`，传入 `--job-module` 和 `--job-id`；状态枚举 `pending → running → completed/failed`，写入 `jobs/status/{job_id}.json`。

**`_post_init` 异步钩子**：`TelegramBotBase.run()` 在 `Application` 上注册 `post_init=self._post_init`，在此 async 方法中调用 `setup_extra_handlers()` 和 `setup_job_queue()`，避免 `run_polling` 启动前的事件循环冲突。

**沙箱安全防御**：所有 I/O 使用 `pathlib.is_relative_to()` 强制限制范围（sandbox → `agent_workspace/`，RAG → `knowledge_base/`）。

**持仓记忆格式（Stock Bot 硬性约束）**：`user_profile.json` 中持仓条目 value 必须严格遵循 `"[中文公司名]，X 股，成本 Y"` 格式。`parse_user_profile_to_positions()` 的正则解析器强依赖关键词 `成本`，不可用同义词替换。

### 盘后报告数据流

```
stock_bot/daily_job.py::job_routine()
  ├── fetch_global_market_news()        # 财联社 + 新浪 + 东财 → 去重 → top 200
  ├── fetch_global_indices()            # 沪深300 + HSI + HSTECH + NDX
  ├── load_user_profile() + calculate_portfolio_valuation()
  ├── generate_market_report()          # LLM 推理（120s 超时）
  ├── knowledge_base/盘后日报_*.md      # 归档 RAG 知识库
  ├── notifier.send_market_report_email()
  └── broadcast_to_telegram()          # 跨进程广播
```

### Telegram Bot 渲染管道

```
LLM 输出
  └── translate_to_telegram_html()     # Markdown → Telegram HTML 方言
        └── 检测 Markdown 表格？
              ├── 是 → render_markdown_table_to_image()  # Playwright 3x DSF PNG
              │         └── send_with_caption_split()    # 图片 + caption 自动分段
              └── 否 → 直接 reply_text(parse_mode=HTML)  # 超长自动分段
```
Agent 推理全程通过 `AsyncTelegramCallbackHandler` 实时上报工具调用状态，配合 `keep_typing_action()` 心跳维持"正在输入"状态。

### Docker 服务架构

**Bot 服务**（根目录 `docker-compose.yml`）：

| 服务 | 容器名 | 命令 |
|------|--------|------|
| `stock-daily-job` | `v2-omnistock-daily-job` | `python -m stock_bot.daily_job` |
| `stock-tg-bot` | `v2-omnistock-tg-bot` | `python -m stock_bot.tg_main` |
| `ehs-tg-bot` | `v2-omniehs-tg-bot` | `python -m ehs_bot.tg_main` |

**obs 服务**（`obs/docker-compose.yml`，独立部署）：

| 服务 | 容器名 | 端口 |
|------|--------|------|
| `api` | `obs-api-1` | 8000 |
| `web` | `obs-web-1` | 3100 |
| `db` | `obs-db-1` | 5433 |

各 bot 服务独立挂载 `data/{bot}/` 子目录，数据完全隔离。obs 以只读方式挂载各 bot 的 observability 目录。`.env` 通过 bind mount 注入（不打入镜像）。

### LLM 配置

模型：`MiniMax-M2.7`，接入点：`https://api.minimax.chat/v1`（OpenAI 兼容协议）。

| 场景 | 超时 | 重试 |
|------|------|------|
| 交互式 Agent（tg_main.py） | 90s | 3 次 |
| 盘后报告生成（daily_job.py） | 120s | 3 次 |
| 外部 I/O 工具（tenacity） | — | 3 次，wait 2–10s 指数退避 |

### 持久化目录

| 目录 | 用途 |
|------|------|
| `data/{bot}/agent_workspace/` | AI 沙箱：报告（.md）、K 线图（.png）、临时渲染文件 |
| `data/{bot}/knowledge_base/` | RAG 原始文件（PDF/MD/TXT/CSV）+ 归档盘后日报 |
| `data/{bot}/embeddings/` | FAISS 向量库 L2 缓存（每文档一个子目录，附 meta.json） |
| `data/{bot}/memory/` | `user_profile.json`（LTM）、会话历史 JSON（STM）、`alerts.json`（价格预警）、`transaction_logs.jsonl`（交易流水）、`incident_log.jsonl`（EHS 隐患日志） |
| `jobs/status/` | 研报任务状态文件（`{job_id}.json`，子进程写入） |
| `jobs/logs/` | 研报任务执行日志（`{job_id}.log` + `{job_id}_stderr.log`） |

### 新增 Bot 扩展方法

1. 在项目根新建 `newbot/` 目录（`agent.py`、`tg_main.py`、`daily_job.py`、`tools/`）
2. `agent.py`：定义目录路径（`data/newbot/...`）、System Prompt、调用 `build_agent()` 组装
3. `tg_main.py`：继承 `TelegramBotBase`，实现钩子方法
4. `docker-compose.yml`：新增服务，挂载 `data/newbot/` 下各子目录
5. `.env`：新增 `NEWBOT_TG_BOT_TOKEN` 和 `NEWBOT_ALLOWED_TG_USERS`

---

## obs 子工程细则

可观测性平台：读取各 bot 写出的 JSONL 日志，摄取入 PostgreSQL，通过 Web UI 展示 Token 消耗 / 模型分布 / 费用估算。技术栈：FastAPI + SQLAlchemy async + PostgreSQL（宿主机端口 5433）/ Next.js + TypeScript（宿主机端口 3100）。三容器：`obs-api-1` / `obs-web-1` / `obs-db-1`。

### obs 内部目录

```
obs/
├── api/app/
│   ├── api/router.py          # 所有路由（查询 + ingest 触发 + SSE）
│   ├── db/{models.py, base.py}# ORM 模型 + AsyncSession 工厂
│   ├── ingestion/service.py   # 幂等写入：raw blob + normalized event
│   ├── adapters/
│   │   ├── common.py          # 共享工具：now() / parse_ts()
│   │   ├── mhxy_jsonl.py      # mhxy_bot JSONL 适配器
│   │   └── omnibot_jsonl.py   # stock/ehs bot JSONL 适配器
│   └── schemas/events.py      # Pydantic 模型
└── web/src/
    ├── pages/                 # index.tsx（总览）/ tokens / sessions / tools
    ├── lib/{api.ts, format.ts}# API 封装 + 共享格式化
    └── types/events.ts        # TypeScript 类型
```

### 数据流

```
omnibot 各 bot 写出 JSONL
  data/mhxy/observability/sessions/    → mhxy_jsonl adapter
  data/stock/observability/sessions/   → omnibot_jsonl adapter
  data/ehs/observability/sessions/     → omnibot_jsonl adapter
    ↓
POST /api/ingest/{project}
    ↓
adapters/*.py 解析 → ingestion/service.py 幂等写入 PostgreSQL
    ↓
FastAPI 路由聚合查询 → Next.js 展示
```

### ⚠️ JSONL 字段契约（隐式约束，改字段会静默丢失）

`adapters/omnibot_jsonl.py` 与 `core/observability.py` 之间存在隐式字段契约。修改 `core/observability.py` 任何字段名时，**必须同步更新对应 adapter**，否则字段会静默丢失（不报错）。

关键对应关系：
- `core/observability.py` → `obs/api/app/adapters/omnibot_jsonl.py`
- `mhxy_bot/` 的 JSONL 格式 → `obs/api/app/adapters/mhxy_jsonl.py`

### 关键设计

- **摄取 cursor**：每个 DataSource 存储 `last_sync_cursor`（文件路径→mtime JSON），未变更文件跳过，无需全量扫描。强制全量重扫：`curl -X POST "http://localhost:8000/api/ingest/mhxy?force=true"`。
- **费用计算**：`_COST_CONFIG` 在 `obs/api/app/api/router.py` 顶部定义，仅含按量计费模型。包月模型返回 `cost: null`，前端显示"包月"。新增或切换模型时须同步更新此配置。
- **SSE 实时推送**：`/api/stream` 端点，ingest 完成后广播通知前端自动刷新。
- **timeline 分页**：`GET /sessions/{id}/timeline?limit=200&offset=0`，默认 200 条，最大 500。

### obs 环境变量（`obs/.env`）

```env
DATABASE_URL=postgresql+asyncpg://agent_obs:changeme@db:5432/agent_obs
MHXY_HOST_LOG_DIR=/volume1/server/.openclaw/workspace/projects/omnibot/data/mhxy/observability/sessions
OMNIBOT_STOCK_HOST_LOG_DIR=/volume1/server/.openclaw/workspace/projects/omnibot/data/stock/observability/sessions
OMNIBOT_EHS_HOST_LOG_DIR=/volume1/server/.openclaw/workspace/projects/omnibot/data/ehs/observability/sessions
```

### obs 专属代码规范

- 路由返回 dict，不用 Pydantic `response_model`（灵活迭代阶段）
- 数据库查询统一用 SQLAlchemy ORM，**禁止 raw SQL（`text()`）**
- 格式化函数统一从 `obs/web/src/lib/format.ts` 导入，不在页面内重复定义
- 新增 project 需同时：① 在 `router.py` 加 `run_xxx_ingest()` wrapper，② 前端 `SYNC_FN_MAP` 登记

### obs/web 视觉规约

**色板使用**：见 `globals.css` 顶部 "Semantic colors" 注释块。语义色（`--purple` / `--orange` / `--teal`）只用在已声明的语义上，**不得当装饰色扩散**。agent / model 分类着色用 `--cat-1/2/3` 三色循环，不挪用语义色。

**动效克制 —— 全站任意时刻最多一个 `.pulse-dot` 在跳**。

- 当前唯一合法用法：`index.tsx` 项目卡的"今日有活动"指示（`p.today_sessions > 0`）
- 多个脉冲点同时跳是 dashboard 设计大忌——视觉焦点被到处拽，反而失去"提醒"功能
- 新增任何"实时""进行中""心跳""未读"指示前，**必须先确认**：当前页面是否已有 pulse-dot 在跳？如果有，新指示用其他视觉手段（静态色点、徽章、文字标签、`opacity` 微调），不要叠加脉冲
- 适用范围：`.pulse-dot` 类、`@keyframes pulse-ring` 派生动画，以及任何模仿 pulse / breathe / glow 的循环动效

如确实需要第二个"动态指示"，先在 PR 描述里论证为何 pulse 是唯一合适的形态，否则默认拒绝。

### ⚠️ obs/web 改动必须在容器内验证（面向 codex / Claude Code）

修改 `obs/web/` 任何文件后，**禁止使用宿主机本地 `npm run dev` / `next build` 验证**。原因：

- 宿主机 NAS 上没有装 Node.js / 没有 `node_modules`
- 即使装了，本地缺少 `obs-api-1` 容器和 `obs-db-1` PostgreSQL，前端拉接口必然失败
- 容器内已通过 `Dockerfile.dev` + bind mount `./web:/app` 实现热更新——改完源文件容器内自动重编译，**不需要重启容器**

正确的验证流程：

```bash
# 1) 确保 obs 三容器在跑
docker compose -f obs/docker-compose.yml up -d

# 2) 改完代码后查看 web 容器编译日志
docker compose -f obs/docker-compose.yml logs web --tail=80

# 3) 浏览器访问验证
#    桌面：http://localhost:3100
#    移动：DevTools 设备模拟（iPhone 13: 390×844）

# 4) 类型检查（容器内跑，本地没有 typescript）
docker compose -f obs/docker-compose.yml exec web npx tsc --noEmit

# 5) 生产构建验证（可选，编译产物问题往往只在 build 时暴露）
docker compose -f obs/docker-compose.yml exec web npm run build
```

仅以下情况才需要重启 web 容器：
- 改了 `package.json` / `next.config.js` / `tsconfig.json`
- 改了 `Dockerfile.dev`
- 改了 `docker-compose.yml` 的 web 服务定义

其他所有改动（`*.tsx` / `*.ts` / `*.css`）都靠 Next.js HMR + bind mount 自动生效，**不要** `docker compose restart web`——重启会丢 HMR 状态，反而拖慢迭代。

api 改动同理：源文件 bind mount，但 FastAPI 没有 HMR，改完用 `docker compose -f obs/docker-compose.yml restart api`。
