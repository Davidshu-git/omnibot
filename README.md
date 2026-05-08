# OmniBot

> 生产级多智能体平台 — 共享 Core 架构，三个领域 Bot，全栈可观测性

---

## 这是什么

OmniBot 是一个部署在家庭 NAS 上、**持续运行中的多 Agent 生产系统**。

三个 Telegram Bot 共用同一套 `core/` 基础设施，覆盖量化分析、安全合规、游戏自动化三个完全不同的业务领域，并配套一个 **FastAPI + Next.js 可观测性平台**，实时追踪每次 LLM 调用的 Token 消耗与费用。

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                        omnibot monorepo                     │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────────┐  │
│  │ stock_bot│  │  ehs_bot │  │        mhxy_bot          │  │
│  │ 量化助理  │  │ 安全合规  │  │  游戏自动化（Windows远程）│  │
│  └────┬─────┘  └────┬─────┘  └────────────┬─────────────┘  │
│       │              │                      │               │
│  ─────┴──────────────┴──────────────────────┴─────────────  │
│                      core/  共享基础层                       │
│   TelegramBotBase · AgentFactory · RAG · Memory · Sandbox   │
│  ─────────────────────────────────────────────────────────  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              obs/  可观测性平台                        │   │
│  │     JSONL → FastAPI → PostgreSQL → Next.js Dashboard │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## 三个 Agent

### OmniStock — 量化股票助理

实时对话式股票分析，覆盖 A 股 / 港股 / 美股，每日 15:30 自动执行盘后研报。

| 能力 | 实现 |
|------|------|
| 多市场查价 | yfinance + akshare 双源，自动识别市场后缀 |
| K 线图 | mplfinance 生成，Telegram 直接发送 |
| 持仓估值 | 多币种实时折算 CNY，Playwright 渲染表格图 |
| 价格预警 | 5 分钟轮询，跌破/突破触发 Telegram 推送 |
| 盘后研报 | 多源资讯聚合去重 → LLM 生成 → 邮件+Telegram 双渠道推送 |
| RAG 知识库 | 历史日报自动归档，支持 PDF/MD 上传即用 |

### OmniEHS — 安全合规助理

EHS（环境健康安全）领域专业 Agent，覆盖法规查询、化学品 GHS 数据、隐患管理。

| 能力 | 实现 |
|------|------|
| 法规检索 | 联网搜索 GB/ISO 标准解读 |
| GHS 查询 | 按化学品名称或 CAS 号查危害分类及 SDS |
| 隐患日志 | 6 级严重度追记，支持按日期/等级/关键词查询 |
| 作业许可 | 内置动火/高处/有限空间/临时用电/吊装检查清单 |

### OmniMHXY — 游戏自动化 Bot

通过 SSH 控制 Windows 端 Executor，结合 OCR + VL 大模型，实现梦幻西游日常任务的无人值守。

| 能力 | 实现 |
|------|------|
| 远程执行 | SSH 免密连接 Windows，HTTP API 控制点击/截图 |
| 视觉感知 | Qwen3-VL 多模态模型识别游戏界面 |
| 状态机 Runner | 确定性主流程 + VL 兜底，可重试、可观测 |
| Watchdog | 独立容器健康监测，失败自动重启 Executor |

---

## 核心设计

### 共享 Core + 领域子类

所有 Bot 继承 `TelegramBotBase`，通过钩子方法注入业务差异，无需重复实现鉴权、渲染、Agent 调度等基础设施。

```python
class StockBot(TelegramBotBase):
    def get_bot_name(self) -> str: return "OmniStock"
    def setup_job_queue(self, app): ...    # 价格预警轮询
    def handle_custom_cmd(self, cmd, ...): ...  # /portfolio /report /alert
```

新增一个 Bot 只需实现业务钩子，复用全部 `core/` 能力。

### L1/L2 混合 RAG 缓存

```
查询请求
  → L1: 进程内 lru_cache（毫秒级）
  → L2: FAISS 硬盘索引（mtime 校验热更新）
  → miss: 重建向量库，双向写入 L1 + L2
```

每日盘后报告自动归档进 `knowledge_base/`，RAG 数据随时间持续累积。

### 双轨记忆

- **STM**：`FileChatMessageHistory` 滑动窗口（10 条），跨重启持久化
- **LTM**：`user_profile.json` KV 状态机，`filelock` 保障并发原子写

### 研报独立子进程

`trigger_job` 通过 `subprocess.Popen` 投递研报任务至独立进程，Agent 不阻塞；`query_job_status` 工具异步轮询状态文件，用户随时查询进度。

### Telegram 渲染管道

```
LLM 输出 → Markdown → HTML 方言转换
  → 检测表格 → Playwright 3x DSF 高清 PNG
  → 自动分段发送（消息长度限制）
```

全程 `AsyncTelegramCallbackHandler` 实时上报工具调用状态，"正在输入"心跳不断。

### 全链路可观测性

每次 `execute_agent_task` 自动写 JSONL 日志（`session / message / thought / model_call / tool_call / tool_result`），由 `obs/` 摄取入 PostgreSQL，Next.js 看板展示 Token 趋势与费用分布。

---

## obs/ 可观测性平台

```
obs/
├── api/         FastAPI + SQLAlchemy async + PostgreSQL
└── web/         Next.js 统计看板（端口 3100）
```

- **幂等摄取**：按内容 hash 去重，重复执行安全
- **增量同步**：cursor 记录文件 mtime，未变更文件直接跳过
- **SSE 推送**：ingest 完成后实时通知前端刷新
- **费用计算**：按量计费模型精确到分，包月模型标注"包月"

---

## 技术栈

| 类别 | 技术 |
|------|------|
| Agent 编排 | LangChain · LangChain Callbacks |
| LLM | MiniMax-M2.7 · Qwen3-VL（DashScope） |
| Telegram | python-telegram-bot 22.x |
| 渲染 | Playwright（表格 → 高清 PNG） |
| 金融数据 | yfinance · akshare · mplfinance |
| 向量检索 | FAISS · DashScope Embeddings |
| 可观测性 | FastAPI · PostgreSQL · Next.js · SQLAlchemy async |
| 容错 | tenacity 指数退避 · filelock 原子写 |
| 部署 | Docker · Docker Compose |

---

## 快速启动

```bash
# 1. 配置环境变量
cp .env.example .env   # 填入 API Key 和 Bot Token

# 2. 构建并启动 Bot 服务
docker build -f Dockerfile.base -t omnibot-base:latest .
docker compose up -d

# 3. 启动可观测性平台（独立 Compose）
docker compose -f obs/docker-compose.yml up -d

# 查看日志
docker compose logs -f
docker compose -f obs/docker-compose.yml logs api -f
```

Bot 服务容器：

| 容器 | 说明 |
|------|------|
| `v2-omnistock-tg-bot` | OmniStock Telegram Bot |
| `v2-omnistock-daily-job` | 盘后调度器（每日 15:30） |
| `v2-omniehs-tg-bot` | OmniEHS Telegram Bot |
| `v2-omnimhxy-tg-bot` | OmniMHXY 游戏控制 Bot |
| `obs-api-1 / obs-web-1 / obs-db-1` | 可观测性平台 |
