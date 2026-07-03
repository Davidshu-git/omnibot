# obs ↔ bot 对话通道运维手册

obs 会话时间线可以通过 bot 容器内嵌 HTTP 服务直接向 bot 发纯文本消息。该通道复用 Telegram bot 进程内的同一个 agent、同一个 `tg_session_{user_id}` 短期记忆文件，以及同一份 `user_profile.json` 长期记忆。

## 端口与服务

| Bot | 根 compose 服务 | 宿主机端口 | 容器端口 |
|-----|-----------------|------------|----------|
| OmniStock | `stock-tg-bot` | `8810` | `8810` |
| OmniEHS | `ehs-tg-bot` | `8811` | `8811` |
| OmniMHXY | `mhxy-tg-bot` | `8812` | `8812` |

bot 侧健康检查：

```bash
curl http://127.0.0.1:8810/healthz
curl http://127.0.0.1:8811/healthz
curl http://127.0.0.1:8812/healthz
```

## HTTP 接口一览

bot 内嵌 HTTP 服务(`core/tg_base.py::_start_obs_chat_http_server`)暴露以下路由,obs 侧通过 `obs/api/app/api/router.py` 的代理端点访问:

| bot 路由 | 方法 | obs 代理端点 | 用途 |
|----------|------|--------------|------|
| `/healthz` | GET | — | 健康检查(无需 token) |
| `/chat` | POST | `/api/external/{project}/chat` | 时间线直接对话 bot,复用同一 agent / 记忆 |
| `/switch-model` | POST | `/api/external/{project}/switch-model` | obs 总览页点击芯片热切换主控/视觉模型 |
| `/executor-power` | POST | `/api/external/mhxy-executor/power` | obs 实例页开关 Windows Executor(仅 mhxy) |
| `/refresh-portfolio` | POST | `/api/external/{project}/refresh-portfolio` | obs 投资总控台「⟳ 重新估值」联网重新取价 + 覆盖当天快照(仅 stock) |
| `/stock-trend` | POST | `/api/external/{project}/stock-trend` | obs 投资总控台「个股趋势分析」弹窗,取价 + 计算 MA20/60/250(仅 stock) |

> 所有 POST 动作端点共用 `core/tg_base.py::_start_obs_chat_http_server` 内的 `obs_action` 装饰器统一做 token 鉴权 + JSON 解析 + 错误短路,handler 只写业务逻辑,请求体从 `request["obs_body"]` 取。**新增动作端点一律套用此装饰器,勿再手抄鉴权/解析样板。**

`/executor-power` 契约:

- 鉴权:同 `/switch-model`,请求头 `X-OBS-Token` == `OBS_BOT_CHAT_TOKEN`。
- 请求体:`{"enabled": true | false}`(必须 bool,否则 422)。
- 仅 mhxy bot 生效:bot 侧读 `EXECUTOR_POWER_FILE` env(`docker-compose.yml` 仅 mhxy-tg-bot 配置),未配置返回 404。bot 把开关原子写入 `data/mhxy/config/executor_power.json`(该目录 mhxy-tg-bot 与 watchdog 均 RW 挂载,obs-api 为只读故不能自行写)。
- watchdog 用 `interruptible_wait()` 1s 粒度监听 `executor_power.json` mtime(无第三方 inotify 依赖),flag 变更 **≤1s 唤醒**(非 60s 轮询)。`read_power_enabled()`:`enabled:false` → `stop_executor()` 杀进程、写 status `disabled`、**跳过自动重启**;`enabled:true` 且上一轮为 `disabled`(重新开启瞬间) → **本轮立即重启**(不等 `FAIL_THRESHOLD` 累积)。实测:禁用反应 ≤1s;重新开启端到端恢复 ~13s(含 executor 冷启动)。文件缺失/损坏一律视为启用(向后兼容)。
- obs 代理广播 `executor_status` SSE。前端 `executor-instances` 页用乐观态 + `powerPending` 过渡锁:点击后立即反馈、过渡期按钮禁用并轮询 watchdog 状态对账(90s 兜底超时),失败回滚——避免「看似没反应 → 反复点」。

`/refresh-portfolio` 契约:

- 鉴权:请求头 `X-OBS-Token` == `OBS_BOT_CHAT_TOKEN`(**不需要 body**,`obs_action(parse_json=False)`)。
- 仅 stock bot 生效:由基类钩子 `TelegramBotBase.refresh_portfolio_snapshot()` 暴露,基类默认返回 `None` → **404**(ehs/mhxy 未覆写)。`StockBot` 覆写:`run_in_executor` 线程池跑同步且联网的 `stock_bot.snapshot.write_snapshot()`(yfinance/akshare 取价,**不能在事件循环里直跑否则 bot 卡死**),同日去重覆盖当天那行,不膨胀。
- **60s 冷却**(bot 侧内存时间戳强制):冷却中返回 `{"status":"cooldown","retry_after":N}` → HTTP **429**;成功返回 `{"status":"ok","date","generated_at","total_market_value"}` → 200;取价/落盘异常返回 `{"status":"error","detail"}` → 500。
- obs 代理超时给 **45s**(取价含 tenacity 重试可能十几秒),成功后广播 `portfolio_refreshed` SSE。
- 前端「⟳ 重新估值」与「↻ 刷新」**语义两分**:刷新=纯重读已落盘快照(秒回),重新估值=触发本端点联网重算。

`/stock-trend` 契约:

- 鉴权:请求头 `X-OBS-Token` == `OBS_BOT_CHAT_TOKEN`。
- 请求体:`{"ticker": "<代码>"}`(必须非空字符串,否则 422)。
- 仅 stock bot 生效:由基类钩子 `TelegramBotBase.get_stock_trend()` 暴露,基类默认返回 `None` → **404**(ehs/mhxy 未覆写)。`StockBot` 覆写:`run_in_executor` 线程池跑 `valuation_engine.fetch_stock_trend()`(yfinance 取近 2 年日线 + 计算 MA20/60/250 + 偏离度历史分位),**不能在事件循环里直跑**。
- **5 分钟内存缓存**(按格式化后 ticker 为 key,bot 侧惰性初始化,非 60s 冷却限流而是纯防抖):同一 ticker 5 分钟内重复请求直接返回缓存,不重复打 yfinance。无 cooldown 语义(不返回 429)。
- 成功返回 `{"status":"ok","ticker","latest_price","latest_date","series"[{date,close,ma20,ma60,ma250}],"ma20"/"ma60"/"ma250":{available,value,direction,slope_pct,deviation_pct,deviation_percentile},"regime_note"}` → 200;取价/计算异常返回 `{"status":"error","detail"}` → 500。
- 产出**仅为描述性指标**(均线方向 + 偏离度历史分位 + 一句 `regime_note` 观察),不产出任何买卖建议措辞,前端固定展示免责声明。
- obs 代理超时给 **20s**(单次 yfinance 拉取,比重估值轻),**不广播 SSE**(纯读查询,只对发起请求的客户端有意义)。

`/switch-model` 契约:

- 鉴权:请求头 `X-OBS-Token` == `OBS_BOT_CHAT_TOKEN`(与 `/chat` 同闸门,**不需要 user_id**,模型是 bot 全局状态)。
- 请求体:`{"kind": "text" | "vl", "model_key": "<key>"}`。`kind` 缺省为 `text`;`vl` 仅 mhxy 支持。
- bot 侧由 `TelegramBotBase.get_model_registries()` 钩子暴露 registry(mhxy 返回 `{text, vl}`,stock/ehs 仅 `{text}`),调用 `registry.switch()` 热生效,**无需重启 bot**。
- 切换成功后 obs 代理广播 `model_switched` SSE,所有连接的 obs 客户端自动刷新 `/api/projects/runtime-models`。
- `{project}` 用 obs↔bot chat 的 key(`stock-bot` / `ehs-bot` / `mhxy-bot`);前端 overview 的 `mhxy` 经 `CHAT_PROJECT` 映射为 `mhxy-bot`。

> ⚠️ 改动 `core/tg_base.py` 或任一 `*_bot/tg_main.py` 后,新路由/钩子需 **重启对应 bot 容器**才生效(`./core`、`./*_bot` 为 bind mount,无需重建镜像):
> `docker restart v2-omnistock-tg-bot v2-omniehs-tg-bot v2-omnimhxy-tg-bot`
> 注:`core/tg_base.py` 为三 bot 共用基类,改它需**三个 bot 都重启**;obs-api 代理改动另需 `docker compose -f obs/docker-compose.yml restart api`。

## 新增一个 obs→bot 动作端点(六步 checklist)

把「重新估值」当范例,后续在总控台加任何定制动作(手动调仓建议、一键研报、面板改持仓/现金、设预警……)都照此六步,不要另起炉灶:

1. **基类钩子**(`core/tg_base.py`):加一个 `async def xxx(self) -> Optional[dict]` 钩子,基类默认 `return None`(不支持 → 404),保持 tg_base 对具体 bot 解耦。
2. **子类覆写**(对应 `*_bot/tg_main.py`):实现业务逻辑。**任何同步/联网/CPU 重活必须 `loop.run_in_executor(None, fn)`**,严禁在事件循环里直跑(会卡死与 Telegram polling 共用的 loop)。需要防抖就在子类加内存时间戳冷却。
3. **bot 路由**(`_start_obs_chat_http_server`):加 `@obs_action()`(或 `parse_json=False`)装饰的 handler,只调钩子 + 映射 status→HTTP 码,注册到 `app.router.add_post`。
4. **obs 代理**(`obs/api/app/api/router.py`):照抄 `proxy_switch_model` / `proxy_refresh_portfolio` 加 `/external/{project}/xxx`,透传 `X-OBS-Token`,按需广播 SSE;超时按后端耗时给足。
5. **前端**(`obs/web/src/lib/api.ts` + 页面):加 `api.xxx()`,页面加按钮/交互,处理 429 冷却与错误提示。
6. **生效 + 登记**:重启相关容器(bot 三个 / obs-api),端到端联调(token 401 / 成功 200 / 未覆写 bot 404 / 冷却 429),然后回本手册路由表 + 契约登记。

## 环境变量

根目录 `.env` 与 `obs/.env` 必须配置同一个共享密钥：

```env
OBS_BOT_CHAT_TOKEN=<同一随机串>
```

`obs/.env` 还应按真实 NAS 内网 IP 配置 bot URL：

```env
STOCK_BOT_CHAT_URL=http://192.168.1.100:8810
EHS_BOT_CHAT_URL=http://192.168.1.100:8811
MHXY_BOT_CHAT_URL=http://192.168.1.100:8812
```

密钥只写入 `.env`，不要写入源码、镜像或手册。

## 部署

`requirements.txt` 新增了 `aiohttp`。首次部署或依赖变更后需要重建 base 镜像并重启服务：

```bash
docker build -f Dockerfile.base -t omnibot-base:latest .
docker compose up -d --build
docker compose -f obs/docker-compose.yml up -d --build
```

## 排障

- `/healthz` 不通：先看 bot 是否读取到 `OBS_BOT_CHAT_TOKEN`，未配置 token 时 HTTP 服务不会启动。
- obs 页面返回 401：根 `.env` 与 `obs/.env` 的 `OBS_BOT_CHAT_TOKEN` 不一致。
- obs 页面返回 403：session 中解析出的 `user_id` 不在对应 bot 白名单。
- obs 页面返回 502：检查 `STOCK_BOT_CHAT_URL` / `EHS_BOT_CHAT_URL` / `MHXY_BOT_CHAT_URL` 是否指向宿主机可访问的内网地址与端口。
- obs 页面返回 504：bot agent 本轮推理超时，需查看对应 bot 容器日志。
- `/switch-model` 返回 404：bot 进程仍是旧代码（未含该路由），需 `docker restart` 对应 bot 容器；或 `kind` 在该 bot 不支持（如对 stock/ehs 传 `vl`）。
- `/switch-model` 返回 422：`model_key` 不在该 registry 的可选列表内，或 `kind` 非 `text`/`vl`。
- `/executor-power` 返回 404：bot 进程仍是旧代码（未含该路由）需重建 mhxy-tg-bot；或 `EXECUTOR_POWER_FILE` env 未配置（仅 mhxy-tg-bot 应配置，改了 `docker-compose.yml` 需 `docker compose up -d mhxy-tg-bot` 重建而非 restart）。
- 开关点了没反应：正常 ≤1s 唤醒 watchdog；若超时未对账（前端提示「未在 90s 内确认」），确认 `data/mhxy/config/executor_power.json` 已更新、`v2-omnimhxy-executor-watchdog` 在跑且为新代码（改 `watchdog.py` 后需 `docker restart` 该容器，无 HMR）。
- `/refresh-portfolio` 返回 404：非 stock bot（ehs/mhxy 未覆写钩子，正常）；或 stock 进程仍是旧代码未含钩子，需 `docker restart v2-omnistock-tg-bot`。
- `/refresh-portfolio` 返回 429：60s 冷却内重复触发，`retry_after` 为剩余秒数，属正常限流。
- `/refresh-portfolio` 返回 500 或 obs 代理 502/超时：查 `v2-omnistock-tg-bot` 日志，多为 yfinance/akshare 取价失败或持仓记忆为空（`write_snapshot()` 返回 None）。
- `/stock-trend` 返回 404：非 stock bot（ehs/mhxy 未覆写钩子，正常）；或 stock 进程仍是旧代码未含钩子，需 `docker restart v2-omnistock-tg-bot`。
- `/stock-trend` 返回 500 或 obs 代理 502/超时：查 `v2-omnistock-tg-bot` 日志，多为该 ticker 在 yfinance 查无历史数据（代码格式错误 / 已退市）。
- `/stock-trend` 返回数据但某条均线 `available:false`：该标的历史不足以计算对应周期均线（如新上市标的不足 250 个交易日算不出 MA250），前端已优雅降级为提示文案，非故障。
