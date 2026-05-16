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
