# Architecture

## 1. 定位

`agent-observability` 是一个面向内部自用的多智能体统一观测平台。

目标：
- 统一监控多个基于 LangChain 构建的智能体项目
- 聚合本地 JSONL 日志与 LangSmith tracing
- 提供统一的多项目、多 Agent、多会话控制台

第一阶段范围：
- 只读监控
- 内网部署
- 单机 Docker Compose

---

## 2. 系统边界

平台不是 Agent 运行框架，也不是 LangSmith 替代品。

平台负责：
- 数据采集
- 数据归一化
- 查询聚合
- 自定义展示

平台不负责：
- Agent 执行
- 任务调度
- 控制写操作
- 权限系统

---

## 3. 总体架构

```text
  +--------------------------+
  | Existing Agent Projects  |
  |--------------------------|
  | mhxy                     |
  | other langchain agents   |
  +------------+-------------+
               |
      +--------+--------+
      |                 |
      v                 v
  +--------+      +-------------+
  | JSONL  |      | LangSmith   |
  | Logs   |      | API         |
  +--------+      +-------------+
      |                 |
      +--------+--------+
               |
               v
  +-----------------------------+
  | Ingestion / Adapter Layer   |
  |-----------------------------|
  | mhxy_jsonl_adapter          |
  | generic_jsonl_adapter       |
  | langsmith_adapter           |
  +-------------+---------------+
                |
                v
  +-----------------------------+
  | Normalization + Persistence |
  | PostgreSQL                  |
  +-------------+---------------+
                |
                v
  +-----------------------------+
  | Query / Aggregation API     |
  | FastAPI                     |
  +-------------+---------------+
                |
                v
  +-----------------------------+
  | Web Control Plane           |
  | Next.js + React             |
  +-----------------------------+
```

---

## 4. 核心设计原则

### 4.1 统一 schema，灵活接入

平台内部使用统一 schema。

外部项目：
- 不强制立即统一日志结构
- 通过 adapter 接入

### 4.2 原始数据、标准化事件、聚合结果三层分离

必须区分：

1. 原始数据
2. 归一化事件
3. 聚合统计

否则后续无法稳定重放与修复。

### 4.3 LangSmith 是数据源

LangSmith 提供：
- tracing
- run tree
- usage

平台负责：
- 多项目聚合
- 自定义 think 展示
- 自定义业务事件融合

### 4.4 先读通，再抽象

第一阶段优先：
- `mhxy` 接入
- schema 稳定
- 查询稳定

不优先：
- 高级前端效果
- 告警
- 控制写操作

---

## 5. 技术栈

### 5.1 前端

- `Next.js`
- `React`
- `TypeScript`

原因：
- 更适合复杂仪表盘与多页控制台
- 更适合长期演进

### 5.2 后端

- `FastAPI`

原因：
- 适合做 ingestion、aggregation、REST API
- 便于数据同步任务与 adapter 组织

### 5.3 数据库

- `PostgreSQL`

原因：
- 支持结构化查询
- 支持 JSON 字段
- 支持索引与聚合

### 5.4 部署

- `Docker Compose`

建议服务：
- `web`
- `api`
- `db`
- 可选 `worker`

---

## 6. 运行时模块

### 6.1 Adapter Layer

负责：
- 读取外部日志与 trace
- 转换为统一 schema
- 补全 ID 与来源字段

不负责：
- 统计
- 展示
- 跨源关联逻辑

### 6.2 Ingestion Layer

负责：
- 接收 adapter 输出
- 去重
- 幂等写入
- 存原始数据
- 存标准化事件

### 6.3 Query Layer

负责：
- 会话列表
- 时间线
- trace 详情
- token 统计
- tool 聚合
- think 查询

### 6.4 UI Layer

负责：
- 项目、Agent、会话、Trace 逐层浏览
- 可视化展示
- 对比与筛选

---

## 7. 关键对象边界

### 7.1 Project

被监控的业务项目。

例如：
- `mhxy`
- `customer-support-bot`

### 7.2 Agent

项目中的智能体实例或逻辑角色。

例如：
- `game-bot`
- `planner`
- `executor`

### 7.3 Session

业务视角的一次会话。

前端主要围绕 session 展开。

### 7.4 Trace

一次 agent 执行链路。

一个 session 可以包含多个 trace。

### 7.5 Run

trace 内部的模型或工具执行单元。

---

## 8. 平台演进路线

### Phase 1

- 接入 `mhxy`
- 建立统一 schema
- 落库
- 基础页面

### Phase 2

- 接入 LangSmith
- 统一 trace 详情页
- token / tool / think 专题页

### Phase 3

- 接入第二个项目验证通用性
- 完善 adapter 机制

### Phase 4

- 告警
- 权限
- 更复杂的统计与对比

---

## 9. 非目标

第一阶段不做：

- WebSocket 实时推送
- 控制动作
- 多租户
- 复杂权限
- 高级告警系统
- 可视化拖拽配置
