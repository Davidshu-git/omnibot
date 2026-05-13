#!/usr/bin/env bash
# 同步 Windows executor 代码并优雅重启。
# 用法：bash ops/deploy_executor.sh [文件1 文件2 ...]
# 不传参数时默认只同步 mhxy_bot/executor/main.py。
set -euo pipefail

SSH_KEY="/home/shudawei/.ssh/id_towin"
WIN="sdw@192.168.100.149"
SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=no -o BatchMode=yes"
REMOTE_DIR="C:/Users/sdw/mhxy_executor"
HEALTH_URL="http://192.168.100.149:8765/health"
REMOTE_LOG="C:\\Users\\sdw\\mhxy_executor\\stderr-watchdog.log"
REMOTE_START_CMD="C:\\Users\\sdw\\mhxy_executor\\start_executor.cmd"

# 默认同步文件列表
FILES=("${@:-mhxy_bot/executor/main.py}")

# ── 1. 同步文件 ──────────────────────────────────────────────────────────────
echo "[1/4] 同步文件到 Windows executor ..."
for f in "${FILES[@]}"; do
  filename=$(basename "$f")
  echo "  scp $f → $REMOTE_DIR/$filename"
  scp $SSH_OPTS "$f" "$WIN:$REMOTE_DIR/$filename"
done

# ── 2. 杀旧进程 ──────────────────────────────────────────────────────────────
echo "[2/4] 杀旧进程 ..."
ssh $SSH_OPTS "$WIN" "taskkill /IM python.exe /F" 2>&1 || true
# 注意：不要 taskkill cmd.exe。start_executor.cmd 的父 cmd.exe 会在 python 退出后自然结束。
# 强杀 cmd.exe 会导致 log 文件句柄未正常释放，下次 VBScript 启动时 >> 重定向失败。
sleep 4

# 确认端口已释放（EADDRINUSE 是最常见的启动失败原因）
for attempt in 1 2 3; do
  in_use=$(ssh $SSH_OPTS "$WIN" \
    "powershell -NoProfile -Command \"netstat -ano | findstr :8765 | findstr LISTENING\"" 2>/dev/null || true)
  if [[ -z "$in_use" ]]; then
    break
  fi
  if [[ $attempt -eq 3 ]]; then
    echo "  ERROR: 端口 8765 仍被占用，无法启动。当前占用信息："
    echo "  $in_use"
    exit 1
  fi
  echo "  端口仍被占用，等待 5s (尝试 $attempt/3) ..."
  sleep 5
done

# ── 3. 通过 WMI Win32_Process.Create 启动（彻底脱离 SSH Job Object）─────────
# 背景：Windows OpenSSH 把会话内所有子进程放进同一 Job Object，SSH 客户端断开
# 时整个 Job 被终止——VBScript / Start-Process 都救不了。WMI 创建的进程不继承
# 父 Job，是目前实测唯一能真正脱离 SSH 会话独立存活的方式。
echo "[3/4] 通过 WMI Win32_Process.Create 启动 executor ..."
PS_LAUNCH=$(printf "%s" "\$ProgressPreference='SilentlyContinue'; Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c $REMOTE_START_CMD'} | Out-Null" \
  | iconv -t UTF-16LE | base64 -w0)
ssh $SSH_OPTS "$WIN" "powershell -NoProfile -EncodedCommand $PS_LAUNCH"

# ── 4. 健康验证（最多 40s）──────────────────────────────────────────────────
echo "[4/4] 等待健康检查 (最多 40s) ..."
for i in $(seq 1 8); do
  sleep 5
  if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
    echo ""
    echo "✓ executor 已上线："
    curl -s "$HEALTH_URL"
    echo ""
    exit 0
  fi
  echo "  尝试 $i/8 ..."
done

echo ""
echo "ERROR: executor 未在 40s 内响应。最近日志："
ssh $SSH_OPTS "$WIN" "type $REMOTE_LOG" 2>/dev/null | tail -15 || true
exit 1
