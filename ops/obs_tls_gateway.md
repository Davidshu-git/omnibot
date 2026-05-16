# obs TLS 网关运维手册

## 这是什么 / 为什么

obs/web 截图巡检页的 H264 推流依赖浏览器 WebCodecs（`VideoDecoder`）。该 API 被规范标记为
`[SecureContext]`，**Chrome/Blink 严格执行**：明文 http 局域网入口下根本不挂载
`window.VideoDecoder`，前端检测失败 → 显示"浏览器不支持 WebCodecs"，推流不可用。
（Safari/WebKit 未严格执行，所以同一 http 入口 Safari 能用、Chrome 不能——这恰是该问题的判据。）

解决方案：用 Caddy 给 obs 提供统一 **https** 入口，所有子连接经此终结 TLS，内部继续走明文。
浏览器进入 Secure Context，WebCodecs 恢复可用。

## 架构

```
Caddy 容器 obs-caddy-1 (镜像 caddy:2-alpine, tls internal 自签 CA)
 ├─ :8443  应用主入口
 │   ├─ /api/*      → api:8000              (直连, flush_interval -1, 顺带修掉 SSE buffering)
 │   ├─ /exec-ws/*  → 192.168.100.149:8765  (输入 + 推流 WS, strip /exec-ws 前缀)
 │   └─ /*          → web:3000              (Next 应用 + HMR)
 └─ :8444  ws-scrcpy 直通 → 192.168.100.149:8000  (根路径, 无前缀, 规避第三方应用路径错位)
```

- 配置文件：`obs/caddy/Caddyfile`（compose `:ro` 挂载，**改完需重启 caddy 容器**）。
- compose 服务：`obs/docker-compose.yml` 的 `caddy`，端口 `8443/8444`，卷
  `caddy_data`（根 CA、证书，**勿删，删了要重装所有设备**）、`caddy_config`。
- **老入口 `web:3100` / `api:8000` 完全保留不动**，本网关并行新增，零回归。
  前端 `obs/web/src/lib/gateway.ts` 按 `window.location.protocol` 派生：https 走网关，
  http 维持历史直连——回滚只需停掉 caddy 容器，老入口照常工作。

## ⚠️ OBS_TLS_HOST：证书 SAN 必须与地址栏完全一致

Caddy `tls internal` 为 `OBS_TLS_HOST`（默认 `192.168.1.100`）签发证书。
**浏览器地址栏输入的 host 必须与此值逐字符一致**，否则证书 SAN 不匹配，直接拒绝连接
（连"点继续"的机会都没有）。

若 obs 实际访问地址不是 `192.168.1.100`（例如 NAS 真实 LAN IP 是别的、或用域名/主机名），
在 `obs/.env` 设：

```env
OBS_TLS_HOST=<用户实际在地址栏输入的 IP 或主机名>
```

然后 `docker compose -f obs/docker-compose.yml up -d caddy`。改完 SAN 变了，
**所有设备需重新走下面的根 CA 安装**（根 CA 不变，只是证书 SAN 变；通常无需重装 CA，
重装 CA 仅在删了 `caddy_data` 卷后才需要）。

访问地址：`https://<OBS_TLS_HOST>:8443`（主站）。8444 端口由前端在网关模式下自动用于
ws-scrcpy，用户无需手动访问。

## 部署 / 重启

```bash
cd /volume1/server/.openclaw/workspace/projects/omnibot

# 首次启动 / 改了 Caddyfile / 改了 compose caddy 服务
docker compose -f obs/docker-compose.yml up -d caddy

# 仅重启（改 Caddyfile 后）
docker compose -f obs/docker-compose.yml restart caddy

# 看日志
docker compose -f obs/docker-compose.yml logs caddy --tail=50
```

## 根 CA 导出与各设备安装（关键人工步骤）

`tls internal` 是私有 CA，设备不信任 → 浏览器报 `NET::ERR_CERT_AUTHORITY_INVALID`。
每台看板设备装一次根 CA 即可（一次性，根 CA 存于 `caddy_data` 卷，重启不变）。

导出根 CA 到当前目录：

```bash
docker compose -f obs/docker-compose.yml exec caddy \
  cat /data/caddy/pki/authorities/local/root.crt > obs-root-ca.crt
```

把 `obs-root-ca.crt` 传到各设备后安装：

- **Windows**：双击 → 安装证书 → 本地计算机 → "受信任的根证书颁发机构" → 完成。
  Chrome/Edge 用系统信任库，重启浏览器生效。
- **macOS**：双击导入"钥匙串访问"→ 系统 → 找到 "Caddy Local Authority"
  → 右键"显示简介"→ 信任 → "始终信任"。Chrome/Safari 用系统信任库。
- **iOS / iPadOS**：AirDrop / 邮件发送 .crt → 设置 → 通用 → VPN 与设备管理 → 安装描述文件
  → **再到** 设置 → 通用 → 关于本机 → 证书信任设置 → 打开对该根证书的完全信任（这一步必须做）。
- **Android**：设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书。注意 Android 7+
  应用默认不信任用户 CA，但浏览器（Chrome）信任用户 CA，看板够用。
- **Firefox（任意平台）**：Firefox 用自有信任库，需在 设置 → 隐私与安全 → 证书 →
  查看证书 → 导入，勾选"信任此 CA 标识网站"。

装完访问 `https://<OBS_TLS_HOST>:8443` 应为绿锁、无警告，截图巡检页 H264 推流恢复。

## 排障

| 现象 | 原因 / 处理 |
|------|------------|
| `NET::ERR_CERT_AUTHORITY_INVALID` | 该设备没装根 CA，按上节安装 |
| `NET::ERR_CERT_COMMON_NAME_INVALID` / 连不上 | 地址栏 host ≠ `OBS_TLS_HOST`，改 env 对齐或用正确地址访问 |
| 页面能开但推流仍"不支持 WebCodecs" | 确认地址栏是 **https**；确认 Chrome 版本 ≥ 94 |
| obs 页面整体 404，`/`、`/sessions`、`/_next/static/chunks/*` 都返回 404 | 常见原因是在正在服务的 `obs-web-1` dev 容器内执行了 `npm run build`，污染 Next dev 的 `.next` 状态。恢复：`docker compose -f obs/docker-compose.yml restart web`。以后不要在常驻 dev 容器里直接跑生产 build；如确需跑，跑完立即重启 web。 |
| 推流/点击无反应 | caddy 容器需能跨网段够到 `192.168.100.149`；验证：<br>`docker compose -f obs/docker-compose.yml exec caddy wget -qO- http://192.168.100.149:8765/health` |
| 命令行 `curl` 测网关报 000/SSL，但浏览器正常 | 已知：本机 curl 的 ClientHello 触发 Caddy internal alert，openssl 与浏览器不受影响。验证用：<br>`printf 'GET / HTTP/1.1\r\nHost: <HOST>\r\nConnection: close\r\n\r\n' \| openssl s_client -connect 127.0.0.1:8443 -servername <HOST> -quiet` |
| 改了 Caddyfile 不生效 | 文件 `:ro` 挂载，必须 `restart caddy`，不会热加载 |

## 回滚

停掉网关即可，前端自动回落老入口（gateway.ts 按协议判定）：

```bash
docker compose -f obs/docker-compose.yml stop caddy
```

老 `http://<host>:3100` / `:8000` 行为完全不受影响。
