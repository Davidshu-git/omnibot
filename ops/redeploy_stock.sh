#!/usr/bin/env bash
# 重启所有依赖 stock_bot/ 代码的容器（防呆：避免只重启一个漏掉另一个）。
#
# 背景：stock_bot/ 通过 bind mount 挂进容器，bind mount 只同步【磁盘文件】，
# 常驻 Python 进程在启动时即把模块 import 进内存，且 Python 不热重载——
# 改完代码不重启进程，跑的还是旧代码。曾因只重启 stock-tg-bot、漏了
# stock-daily-job，导致 15:31 日报用旧代码把 BTC 当美股取价、算出 -99.96%。
#
# 注意：docker compose up -d 对纯 bind-mount 代码改动【不会】重建容器
# （image/config 没变），必须显式 restart。
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICES="stock-tg-bot stock-daily-job"
echo "🔄 重启 stock_bot 相关容器：${SERVICES}"
docker compose restart ${SERVICES}
echo "✅ 完成，当前状态："
docker compose ps ${SERVICES}
