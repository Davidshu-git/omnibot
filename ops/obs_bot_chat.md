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

`/executor-power` 契约:

- 鉴权:同 `/switch-model`,请求头 `X-OBS-Token` == `OBS_BOT_CHAT_TOKEN`。
- 请求体:`{"enabled": true | false}`(必须 bool,否则 422)。
- 仅 mhxy bot 生效:bot 侧读 `EXECUTOR_POWER_FILE` env(`docker-compose.yml` 仅 mhxy-tg-bot 配置),未配置返回 404。bot 把开关原子写入 `data/mhxy/config/executor_power.json`(该目录 mhxy-tg-bot 与 watchdog 均 RW 挂载,obs-api 为只读故不能自行写)。
- watchdog 用 `interruptible_wait()` 1s 粒度监听 `executor_power.json` mtime(无第三方 inotify 依赖),flag 变更 **≤1s 唤醒**(非 60s 轮询)。`read_power_enabled()`:`enabled:false` → `stop_executor()` 杀进程、写 status `disabled`、**跳过自动重启**;`enabled:true` 且上一轮为 `disabled`(重新开启瞬间) → **本轮立即重启**(不等 `FAIL_THRESHOLD` 累积)。实测:禁用反应 ≤1s;重新开启端到端恢复 ~13s(含 executor 冷启动)。文件缺失/损坏一律视为启用(向后兼容)。
- obs 代理广播 `executor_status` SSE。前端 `executor-instances` 页用乐观态 + `powerPending` 过渡锁:点击后立即反馈、过渡期按钮禁用并轮询 watchdog 状态对账(90s 兜底超时),失败回滚——避免「看似没反应 → 反复点」。

`/switch-model` 契约:

- 鉴权:请求头 `X-OBS-Token` == `OBS_BOT_CHAT_TOKEN`(与 `/chat` 同闸门,**不需要 user_id**,模型是 bot 全局状态)。
- 请求体:`{"kind": "text" | "vl", "model_key": "<key>"}`。`kind` 缺省为 `text`;`vl` 仅 mhxy 支持。
- bot 侧由 `TelegramBotBase.get_model_registries()` 钩子暴露 registry(mhxy 返回 `{text, vl}`,stock/ehs 仅 `{text}`),调用 `registry.switch()` 热生效,**无需重启 bot**。
- 切换成功后 obs 代理广播 `model_switched` SSE,所有连接的 obs 客户端自动刷新 `/api/projects/runtime-models`。
- `{project}` 用 obs↔bot chat 的 key(`stock-bot` / `ehs-bot` / `mhxy-bot`);前端 overview 的 `mhxy` 经 `CHAT_PROJECT` 映射为 `mhxy-bot`。

> ⚠️ 改动 `core/tg_base.py` 或任一 `*_bot/tg_main.py` 后,新路由/钩子需 **重启对应 bot 容器**才生效(`./core`、`./*_bot` 为 bind mount,无需重建镜像):
> `docker restart v2-omnistock-tg-bot v2-omniehs-tg-bot v2-omnimhxy-tg-bot`

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
