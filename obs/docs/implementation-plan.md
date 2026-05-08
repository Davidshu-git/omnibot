# Implementation Plan

## 1. 目标

该文档定义第一阶段实施顺序，供编码 agent 直接按阶段推进。

总原则：
- 不并行乱写
- 先打底层协议和存储
- 再做接入
- 再做页面

---

## 2. 阶段划分

### Phase 0: 初始化

输出：
- 项目骨架
- Docker Compose
- 前后端基础脚手架
- PostgreSQL 初始化

任务：
- 创建 `apps/api`
- 创建 `apps/web`
- 创建 `packages/schemas`
- 创建 `packages/adapters`
- 创建数据库迁移目录

验收：
- `docker compose up` 能启动空白前后端和数据库

---

### Phase 1: 落统一 Schema

输出：
- 统一事件类型定义
- Pydantic 模型
- TypeScript 类型

任务：
- 在后端定义 normalized event schema
- 在前端共享类型定义或自动生成类型
- 固定 `event_type` 列表

验收：
- 前后端对同一事件结构理解一致

禁止：
- 此阶段不要写具体页面

---

### Phase 2: 数据库模型

输出：
- 数据库表
- 迁移脚本
- 基础索引

必须实现：
- `projects`
- `agents`
- `data_sources`
- `sessions`
- `events`
- `raw_event_blobs`

建议实现：
- `daily_usage_stats`

验收：
- 能执行迁移
- 能插入一批测试事件
- 基础查询可跑通

---

### Phase 3: Ingestion 基础设施

输出：
- 原始数据写入
- 标准化事件写入
- 幂等去重逻辑

任务：
- 定义 ingestion service
- 设计幂等键
- 支持重复同步不生成重复数据

验收：
- 同一批数据重复 ingest 结果不变

---

### Phase 4: `mhxy` Adapter

输出：
- `mhxy_jsonl_adapter`
- `mhxy` 项目接入

任务：
- 扫描 `logs/sessions/*.jsonl`
- 解析 `session/message/model_call/tool_call/tool_result/thought`
- 映射到统一 schema

验收：
- 能完整导入一批 `mhxy` 历史日志
- 会话列表与时间线正确

注意：
- 保留原始 JSONL 行到 `raw_event_blobs`

---

### Phase 5: 基础查询 API

输出：
- 会话列表 API
- 时间线 API
- trace 详情 API
- token 统计 API
- tool 分析 API
- think 查询 API

最小接口：
- `GET /api/projects`
- `GET /api/projects/{project_id}/agents`
- `GET /api/sessions`
- `GET /api/sessions/{session_id}/timeline`
- `GET /api/traces/{trace_id}`
- `GET /api/stats/tokens/overview`
- `GET /api/stats/tools`
- `GET /api/think`

验收：
- 全部接口返回稳定 JSON
- 支持分页和时间范围过滤

---

### Phase 6: 前端 MVP

输出：
- 全局总览页
- 会话时间线页
- Trace 详情页

任务：

#### 页面 1：总览
- 项目卡片
- Agent 数量
- 今日会话
- 错误数
- token 汇总

#### 页面 2：会话时间线
- 左侧列表
- 右侧时间线
- 支持按事件类型过滤

#### 页面 3：Trace 详情
- trace 树
- run 节点
- tool/model 联动信息

验收：
- 可以完成“从项目 -> 会话 -> trace”的浏览流程

---

### Phase 7: Think / Token / Tool 专题页

输出：
- think 查看页
- token 统计页
- tool 调用分析页

验收：
- 可以独立按项目和时间范围筛选

---

### Phase 8: LangSmith Adapter

输出：
- `langsmith_adapter`
- LangSmith 配置与同步

任务：
- 拉取 traces / runs / usage
- 与 session/trace 对齐
- 支持 trace 详情中的 LangSmith 关联视图

验收：
- 至少一个项目的 LangSmith 数据可导入
- 同一 session 能看到本地日志与 LangSmith 融合结果

注意：
- 先按 API 拉取实现
- 不要一开始深度做复杂缓存和增量优化

---

### Phase 9: 第二项目接入验证

输出：
- `generic_jsonl_adapter` 或第二个项目专用 adapter

目的：
- 验证平台不是只对 `mhxy` 生效

验收：
- 第二个项目能成功进入统一控制台
- 无需修改平台主 schema

---

## 3. 强制实施顺序

后续编码必须按下面顺序执行：

1. `schema`
2. `db`
3. `ingestion`
4. `mhxy adapter`
5. `api`
6. `web mvp`
7. `langsmith`
8. `second project`

禁止跳步骤直接先写页面或先做 LangSmith 深度集成。

---

## 4. 验收里程碑

### Milestone A

- 平台能读 `mhxy`
- 能看会话时间线
- 能看 trace 详情

### Milestone B

- 能看 token / tool / think
- 能稳定增量同步

### Milestone C

- 能接 LangSmith
- 能接第二个项目

---

## 5. 第一阶段不做

明确不做：

- 登录权限
- 告警通知
- 任务回放与控制
- WebSocket 实时更新
- 复杂 DSL 搜索
- 多租户
- 高级看板自定义

这些都不能阻塞 MVP。

---

## 6. 编码注意事项

### 6.1 必须先定义 adapter contract

没有统一 adapter 接口前，不允许开始接多个数据源。

### 6.2 任何同步逻辑都要幂等

必须做到：
- 可重复执行
- 可恢复
- 可重跑

### 6.3 不允许 UI 反向依赖源格式

前端只能依赖统一 schema API。

前端不得直接理解：
- `mhxy` 原始 JSONL 格式
- LangSmith 原始 API 对象

### 6.4 所有项目特有字段只能进 `extra`

避免主 schema 被单项目污染。

---

## 7. 建议交付方式

建议分 PR / 任务包：

1. `schema + db`
2. `ingestion + mhxy adapter`
3. `query api`
4. `web mvp`
5. `token/tool/think pages`
6. `langsmith adapter`
7. `second project adapter`

每一步都应有可运行验收，不建议一次性大重构全部堆进去。
