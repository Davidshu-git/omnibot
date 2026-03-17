# OmniBot：多智能体 Monorepo 框架

OmniBot 是一个基于 **Monorepo + 共享 Core** 设计的多领域 AI Agent 平台。

通过 `core/` 共享层统一封装 Telegram Bot 基础设施、RAG 引擎、记忆系统和沙箱安全，各领域 Bot（OmniStock、OmniEHS）只需聚焦业务逻辑，以子类继承的方式接入统一平台。

---

## 架构概览

```
omnibot/
├── core/                        # 共享基础层（各 Bot 公用）
│   ├── tg_base.py               # TelegramBotBase：渲染、鉴权、Agent 调度
│   ├── agent_base.py            # build_agent()：LangChain Agent 工厂
│   ├── notifier.py              # SMTP 邮件推送（Markdown → HTML）
│   ├── job_runner.py            # 独立子进程研报任务调度器
│   └── tools/
│       ├── file_tools.py        # 沙箱 I/O + L1/L2 混合 RAG 引擎
│       ├── memory_tools.py      # LTM 长期记忆 KV 状态机
│       └── job_tools.py        # 后台任务投递与状态查询
│
├── stock_bot/                   # OmniStock：A股/港股/美股量化助理
│   ├── agent.py                 # Agent 组装入口
│   ├── tg_main.py               # StockBot(TelegramBotBase) 子类
│   ├── daily_job.py             # 盘后调度器（每日 15:30）
│   ├── valuation_engine.py      # 多币种估值引擎
│   └── tools/stock_tools.py     # 股票领域专属工具
│
├── ehs_bot/                     # OmniEHS：EHS 安全合规专业助理
│   ├── agent.py                 # Agent 组装入口
│   ├── tg_main.py               # EHSBot(TelegramBotBase) 子类
│   ├── daily_job.py             # EHS 定期简报调度器
│   └── tools/ehs_tools.py       # EHS 领域专属工具
│
├── data/
│   ├── stock/                   # OmniStock 持久化数据
│   │   ├── memory/              # LTM（user_profile.json、alerts.json）
│   │   ├── knowledge_base/      # RAG 原始文件（PDF/MD）+ 归档日报
│   │   ├── embeddings/          # FAISS 向量库 L2 缓存
│   │   └── agent_workspace/     # Agent 沙箱（报告、K 线图）
│   └── ehs/                     # OmniEHS 持久化数据（结构同上）
│
├── jobs/
│   ├── status/                  # 研报任务状态（{job_id}.json）
│   └── logs/                    # 研报任务日志（{job_id}.log）
│
├── Dockerfile.base              # 重型基础镜像（pip 依赖 + Playwright）
├── Dockerfile                   # 轻量业务镜像（FROM base + COPY . .）
├── docker-compose.yml           # 三服务编排
└── .env                         # 环境变量（不打入镜像）
```

---

## Bot 功能说明

### OmniStock — 量化股票助理

| Telegram 命令 | 说明 |
|--------------|------|
| `/start` | 打开 Inline Keyboard 操控面板 |
| `/portfolio` | 实时持仓估值（多币种折算 CNY，Playwright 渲染表格图） |
| `/report` | 投递盘后研报生成任务至独立子进程 |
| `/status` | 查询最近一次研报任务状态 |
| `/kb` | 列出知识库文件 |
| `/alert` | 设定盯盘价格预警（跌破/突破，5 分钟轮询） |
| 普通文本 | Agent 推理：查价、K 线图、RAG 研报分析 |
| 文件上传 | 自动入库知识库并触发 RAG 总结 |

**领域专属工具：**

- `get_universal_stock_price` — 美股/A股/港股查价（yfinance + akshare 双源）
- `get_etf_price` — A股 ETF 实时行情
- `draw_universal_stock_chart` — K 线图生成（mplfinance，支持自定义周期）
- `search_company_ticker` — 联网搜索公司股票代码（DDGS）
- `calculate_exact_portfolio_value` — 多币种持仓精确估值（禁止 LLM 心算）
- `create_price_alert` — 创建价格预警规则写入 alerts.json

---

### OmniEHS — EHS 安全合规助理

**领域专属工具：**

- `search_ehs_regulation` — 联网搜索 GB/ISO 标准及法规解读（DDGS）
- `query_chemical_ghs_info` — 按化学品名称/CAS 号查询 GHS 危害分类及 SDS
- `log_incident` — 追加记录隐患/事故至 incident_log.jsonl（6 级严重度）
- `query_incident_log` — 按日期/等级/关键词查询历史隐患统计
- `get_work_permit_checklist` — 获取标准作业许可证检查项（动火/高处/有限空间/临时用电/吊装）

---

## 核心设计亮点

### 共享 Core + 领域子类
`TelegramBotBase` 封装所有 Bot 通用基础设施（渲染管道、白名单鉴权、Agent 异步调度、文件上传入库、跨进程广播接收）。领域 Bot 仅需通过钩子方法注入差异：

```python
class StockBot(TelegramBotBase):
    def get_bot_name(self) -> str: return "OmniStock"
    def get_tool_status_map(self) -> dict: ...   # 工具调用状态文案
    def setup_job_queue(self, app): ...          # 价格预警轮询
    def handle_custom_cmd(self, cmd, ...): ...   # /portfolio /report /alert
```

### Telegram 渲染管道
LLM 输出 → `translate_to_telegram_html()` → 检测 Markdown 表格 → Playwright 渲染高分辨率 PNG（3x DSF）→ `send_with_caption_split()` 自动分段。Agent 推理全程通过 `AsyncTelegramCallbackHandler` 实时上报工具调用状态。

### L1/L2 混合 RAG 缓存
进程内 `lru_cache`（L1）→ FAISS 硬盘（L2，mtime 校验热更新）→ 均未命中则重建并双向写入。每日盘后报告自动归档至 knowledge_base，持续扩充 RAG 数据飞轮。

### 双轨记忆架构
- **STM**：`FileChatMessageHistory` 滑动窗口（10 条）
- **LTM**：`user_profile.json` KV 状态机，`filelock` 保障并发原子写入

### 研报独立子进程
`trigger_job` 工具以 `subprocess.Popen` 启动 `core/job_runner.py`，任务状态写入 `jobs/status/{job_id}.json`，不阻塞主线程。`query_job_status` 工具异步轮询进度。

### 沙箱安全防御
所有 Agent I/O 使用 `pathlib.is_relative_to()` 校验路径层级，强制 sandbox 限制在 `agent_workspace/`，知识库操作限制在 `knowledge_base/` 内。

---

## 部署

### 环境变量（`.env`）

```env
# 必填
DASHSCOPE_API_KEY=           # 主模型推理（Qwen via DashScope）
DASHSCOPE_EMBEDDING_KEY=     # RAG 向量化（text-embedding-v3）

# Stock Bot
TG_BOT_TOKEN=                # BotFather 获取
ALLOWED_TG_USERS=            # 白名单用户 ID，逗号分隔

# EHS Bot
EHS_TG_BOT_TOKEN=
EHS_ALLOWED_TG_USERS=

# 可选（邮件推送）
SMTP_SERVER=
SMTP_PORT=465
SENDER_EMAIL=
SENDER_PASSWORD=
RECEIVER_EMAIL=
```

### Docker 部署（推荐）

```bash
# 首次或依赖变更时，构建基础镜像（含 pip + Playwright，耗时较长）
docker build -f Dockerfile.base -t omnibot-base:latest .

# 启动所有服务
docker compose up -d --build

# 查看日志
docker compose logs -f

# 仅重启某个服务
docker compose restart stock-tg-bot
```

三个服务：

| 服务 | 容器名 | 说明 |
|------|--------|------|
| `stock-daily-job` | `v2-omnistock-daily-job` | 盘后调度器，每日 15:30 自动执行 |
| `stock-tg-bot` | `v2-omnistock-tg-bot` | OmniStock Telegram Bot |
| `ehs-tg-bot` | `v2-omniehs-tg-bot` | OmniEHS Telegram Bot |

### 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
.venv/bin/playwright install chromium

# 启动 Stock Bot
.venv/bin/python -m stock_bot.tg_main

# 启动 EHS Bot
.venv/bin/python -m ehs_bot.tg_main

# 手动触发一次盘后任务（测试）
.venv/bin/python -m stock_bot.daily_job --test
```

---

## 技术栈

| 类别 | 库 |
|------|----|
| Agent 编排 | LangChain, LangGraph |
| LLM | Qwen3.5-Plus（DashScope，OpenAI 兼容协议） |
| Telegram Bot | python-telegram-bot ~= 22.6 |
| 无头浏览器渲染 | Playwright ~= 1.58.0（表格 → PNG） |
| 金融数据 | yfinance, akshare, mplfinance |
| 向量检索 | FAISS, DashScope Embeddings |
| 网络搜索 | ddgs（DuckDuckGo） |
| 容错重试 | tenacity（指数退避） |
| 并发安全 | filelock |
| 邮件推送 | smtplib + markdown |
| 容器化 | Docker, Docker Compose |
