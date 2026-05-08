# CLAUDE.md

本文件为 Claude Code 在 `obs/` 目录工作时的导航手册。

---

## 项目定位

**obs** 是 omnibot 的可观测性平台，读取各 bot 写出的 JSONL 日志，摄取入 PostgreSQL，通过 Web UI 展示 Token 消耗、模型分布、费用估算等统计。

属于 omnibot monorepo 的基础设施层，与 `core/` 平行——`core/` 是共享代码，`obs/` 是共享监控。

---

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI + SQLAlchemy async + PostgreSQL（宿主机端口 5433） |
| 前端 | Next.js + TypeScript（宿主机端口 3100） |
| 部署 | 独立 Docker Compose，三容器：`obs-api-1`、`obs-web-1`、`obs-db-1` |

---

## 常用命令

```bash
# 启动 / 重建
docker compose -f obs/docker-compose.yml up -d --build

# 查看日志
docker compose -f obs/docker-compose.yml logs api -f

# 连接数据库
docker exec obs-db-1 psql -U agent_obs -d agent_obs

# 手动触发摄取
curl -X POST http://localhost:8000/api/ingest/mhxy
curl -X POST http://localhost:8000/api/ingest/stock-bot
curl -X POST http://localhost:8000/api/ingest/ehs-bot

# 强制全量重扫（跳过 cursor 缓存）
curl -X POST "http://localhost:8000/api/ingest/mhxy?force=true"
```

---

## 目录结构

```
obs/
├── api/
│   └── app/
│       ├── api/router.py          # 所有路由（查询 + ingest 触发 + SSE）
│       ├── db/
│       │   ├── models.py          # ORM 模型（Project/Agent/Session/Event/…）
│       │   └── base.py            # AsyncSession 工厂
│       ├── ingestion/service.py   # 幂等写入：raw blob + normalized event
│       ├── adapters/
│       │   ├── common.py          # 共享工具：now() / parse_ts()
│       │   ├── mhxy_jsonl.py      # mhxy_bot JSONL 格式适配器
│       │   └── omnibot_jsonl.py   # stock/ehs bot JSONL 格式适配器
│       └── schemas/events.py      # Pydantic 模型（NormalizedEvent 等）
└── web/
    └── src/
        ├── pages/                 # index.tsx（总览）/ tokens.tsx / sessions.tsx
        ├── lib/
        │   ├── api.ts             # 所有 API 调用封装
        │   └── format.ts          # 共享格式化函数
        └── types/events.ts        # TypeScript 类型定义
```

---

## 数据流

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

---

## JSONL 字段契约

**重要**：`adapters/omnibot_jsonl.py` 与 `core/observability.py` 之间存在隐式字段契约。

修改 `core/observability.py` 中任何字段名时，必须同步更新对应 adapter，否则字段会静默丢失（不报错）。

关键对应关系：
- `core/observability.py` → `obs/api/app/adapters/omnibot_jsonl.py`
- `mhxy_bot/` 的 JSONL 格式 → `obs/api/app/adapters/mhxy_jsonl.py`

---

## 关键设计

**摄取 cursor**：每个 DataSource 存储 `last_sync_cursor`（文件路径→mtime JSON），未变更文件跳过，无需全量扫描。

**费用计算**：`_COST_CONFIG` 在 `api/app/api/router.py` 顶部定义，仅含按量计费模型。包月模型返回 `cost: null`，前端显示"包月"。新增或切换模型时须同步更新此配置。

**SSE 实时推送**：`/api/stream` 端点，ingest 完成后广播通知前端自动刷新。

**timeline 分页**：`GET /sessions/{id}/timeline?limit=200&offset=0`，默认 200 条，最大 500。

---

## 环境变量（`obs/.env`）

```env
DATABASE_URL=postgresql+asyncpg://agent_obs:changeme@db:5432/agent_obs
MHXY_HOST_LOG_DIR=/volume1/server/.openclaw/workspace/projects/omnibot/data/mhxy/observability/sessions
OMNIBOT_STOCK_HOST_LOG_DIR=/volume1/server/.openclaw/workspace/projects/omnibot/data/stock/observability/sessions
OMNIBOT_EHS_HOST_LOG_DIR=/volume1/server/.openclaw/workspace/projects/omnibot/data/ehs/observability/sessions
```

---

## 代码规范

- 路由返回 dict，不用 Pydantic response_model（灵活迭代阶段）
- 数据库查询统一用 SQLAlchemy ORM，禁止 raw SQL（`text()`）
- 格式化函数统一从 `lib/format.ts` 导入，不在页面内重复定义
- 新增 project 需同时：①在 `router.py` 加 `run_xxx_ingest()` wrapper，②前端 `SYNC_FN_MAP` 登记
