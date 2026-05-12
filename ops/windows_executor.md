# Windows Executor 运维手册

Windows executor 运行在 `192.168.100.149`，HTTP 端口 `8765`。
代码目录：`C:\Users\sdw\mhxy_executor\`，启动脚本：`start_executor.cmd`。

---

## 部署 / 更新代码

**推荐方式**：使用 `ops/deploy_executor.sh`，一步完成同步 + 重启 + 验证。

```bash
# 从项目根执行（默认只同步 main.py）
bash ops/deploy_executor.sh

# 同步多个文件
bash ops/deploy_executor.sh mhxy_bot/executor/main.py mhxy_bot/executor/requirements.txt
```

脚本执行顺序：
1. SCP 指定文件到 `C:\Users\sdw\mhxy_executor\`
2. `taskkill /IM python.exe /F`（只杀 python，不杀 cmd.exe，见坑 5）
3. 轮询确认端口 8765 释放（最多 3 次重试）
4. `Invoke-CimMethod Win32_Process Create` 拉起 `start_executor.cmd`（WMI 创建的进程不继承 SSH Job Object，见坑 6）
5. 轮询 `/health` 最多 40s，超时打印日志并退出非零

注意：executor 启动约需 15s（RapidOCR 加载 ONNX 模型），健康检查 3 次尝试内通常能通过。

**手动方式**（排查或脚本不可用时）：

```bash
# 同步单个文件
scp -i /home/shudawei/.ssh/id_towin -o StrictHostKeyChecking=no \
  mhxy_bot/executor/main.py \
  sdw@192.168.100.149:C:/Users/sdw/mhxy_executor/main.py
```

更新后必须重启进程才能生效（Python 不热重载）。

---

## 重启 Executor

**正确流程（三步，缺一不可）：**

```bash
# 第 1 步：杀进程——必须用裸 shell 直接调 taskkill，不能套 PowerShell
ssh -i /home/shudawei/.ssh/id_towin -o StrictHostKeyChecking=no \
  sdw@192.168.100.149 "taskkill /IM python.exe /F"

# 第 2 步：等进程退出
sleep 3

# 第 3 步：通过 WMI Win32_Process.Create 启动（唯一能脱离 SSH Job Object 的方式，见坑 6）
ssh -i /home/shudawei/.ssh/id_towin -o StrictHostKeyChecking=no \
  sdw@192.168.100.149 \
  "powershell -NoProfile -Command \"Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c C:\\Users\\sdw\\mhxy_executor\\start_executor.cmd'} | Out-Null\""

# 验证（executor 启动约 15s，等待时间要足够）
sleep 15
curl http://192.168.100.149:8765/health
```

---

## 已知坑（2026-05-12 实测）

### 坑 1：PowerShell 多命令合并执行静默失败

**现象**：把多条命令塞进一个 `powershell -Command "..."` 里，中间某条失败后后续命令照常执行，但整体无任何输出，无法判断哪步出错。

**正确做法**：排查时拆成单条独立 SSH 调用，每条单独执行确认结果。

---

### 坑 2：PowerShell 包装的 `taskkill` / `Stop-Process` 无法杀进程

**现象**：以下两种写法均无效，进程 PID 不变：
```bash
# 无效
ssh ... "powershell -Command \"Stop-Process -Id 26164 -Force\""
# 无效
ssh ... "powershell -Command \"taskkill /PID 26164 /F\""
```

**原因**：PowerShell 嵌套调用时存在权限或会话隔离问题。`restart_executor()` 原代码也使用了同样的写法，经验证同样无效，已修复为裸 `taskkill`。

**正确做法**：不走 PowerShell 包装，直接调：
```bash
ssh ... "taskkill /PID 26164 /F"
# 或杀全部 python.exe
ssh ... "taskkill /IM python.exe /F"
```

---

### 坑 3：`Start-Process` 直接启动 `.cmd` 文件不生效

**现象**：以下写法进程启动后立即退出：
```powershell
Start-Process -FilePath 'C:\Users\sdw\mhxy_executor\start_executor.cmd' -WindowStyle Hidden
```

**原因**：`.cmd` 文件需要 `cmd.exe` 解释执行，`Start-Process` 无法直接运行。

**正确做法**：
```powershell
Start-Process cmd.exe -ArgumentList '/c C:\Users\sdw\mhxy_executor\start_executor.cmd' -WindowStyle Hidden
```

---

### 坑 4：启动后看似成功但很快静默死亡 → 验证方式靠日志

**现象**：执行重启命令返回正常（无错误输出），stderr 日志甚至能看到完整启动序列：
```
INFO:     Started server process [XXXXX]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8765
```
但十几秒后 `tasklist | findstr python` 进程消失、`curl /health` 超时。

**根因**：见坑 6（Windows OpenSSH Job Object 机制）。**Start-Process / cscript+VBScript 都救不了**，必须用 WMI `Win32_Process.Create()`。

**验证启动是否真的成活**：仅看日志末尾的 "Uvicorn running" 不够，必须等 ≥15s 再 curl `/health`：

```bash
sleep 15 && curl -sf http://192.168.100.149:8765/health
```

成功返回 `{"status":"ok",...}` 才算真稳定。

**EADDRINUSE 错误处理**：若 stderr 日志出现 `[Errno 10048] ... 端口只允许使用一次`，说明 8765 已被占用，需先确认旧进程已彻底退出再重启：

```bash
ssh ... "powershell -NoProfile -Command \"netstat -ano | findstr :8765\""
```

无输出才可以启动新进程。

---

### 坑 5：不能 `taskkill /IM cmd.exe /F`（2026-05-12 实测）

**现象**：deploy 脚本同时杀 `python.exe` 和 `cmd.exe` 后，下一次启动的新 cmd.exe 进入 batch 后 python 进程始终不出现，log 文件也无新写入。

**原因**：`start_executor.cmd` 使用 `>> stderr-watchdog.log` 追加重定向。强杀 cmd.exe（`/F`）导致该文件的 OS 写句柄未经正常 flush/close 直接释放。后续新 cmd.exe 运行同一 batch 文件时，`>>` 追加操作失败（写入目标处于不一致状态），cmd 以非零退出但不输出任何错误，整个进程静默消失。

**正确做法**：只杀 `python.exe`。start_executor.cmd 的父 cmd.exe 会在 python 退出后自然退出，log 文件句柄得到正常释放。

---

### 坑 6：SSH 启动的进程会被 OpenSSH Job Object 一锅端（核心坑，2026-05-12 实测）

**现象**：通过 SSH 调起 `Start-Process` 或 `cscript launch.vbs` 启动 executor，几秒后整个 python 进程链消失，无任何 traceback。即便加 `WScript.Sleep 2000`、`& detach`、`-WindowStyle Hidden` 均无效。

**根因**：Windows OpenSSH (`sshd`) 把同一 SSH 会话中创建的所有子进程放进同一个 **Job Object**。SSH 客户端断开时，Windows 终止整个 Job —— cscript → cmd → python 整条链一起死，**子进程没机会写任何 shutdown 日志**（Job kill 等价于 SIGKILL）。

**唯一可靠的脱离方式**：WMI `Win32_Process.Create()`。WMI 通过本地 RPC 调 `WMI Provider Host` 创建进程，新进程的父进程是 `WmiPrvSE.exe`，不继承调用方 Job。

```powershell
# ✓ 唯一可靠的后台启动方式
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c C:\Users\sdw\mhxy_executor\start_executor.cmd'} | Out-Null

# ✗ 以下方式全部会被 Job 一锅端：
Start-Process cmd.exe -ArgumentList '/c start_executor.cmd' -WindowStyle Hidden
cscript //NoLogo //B launch_executor.vbs        # 无论 VBS 里 sleep 多久
ssh ... "python -m uvicorn ..."                  # 前台运行，SSH 断开即死
```

`schtasks /Run` 也可行（Task Scheduler 拉起的任务不属于 SSH Job），但需要预注册 task，复杂度高于 WMI。

**实测验证**：
- 通过 WMI 启动后立即 SSH 断开 → 进程持续运行（已稳定 3+ 分钟，处理请求正常）
- 通过 VBScript+Sleep 启动后立即 SSH 断开 → 进程在 10s 内消失，stderr 无报错

---

## adb 路径

Windows 上有多个 adb.exe，**唯一可信路径**（实测能正确识别 MuMu 模拟器）：

```
C:\Program Files\Netease\MuMu\nx_main\adb.exe
```

该路径已作为 `executor/main.py`（`ADB_PATH`）和 `watchdog.py`（`WINDOWS_ADB_PATH`）的统一默认值。调试时直接用此路径：

```bash
ssh -i /home/shudawei/.ssh/id_towin -o StrictHostKeyChecking=no \
  sdw@192.168.100.149 \
  "powershell -NoProfile -Command \"& 'C:\\Program Files\\Netease\\MuMu\\nx_main\\adb.exe' devices\""
```

---

## start_executor.cmd 与 watchdog 的关系

`start_executor.cmd` 由 watchdog 的 `restart_executor()` 在每次自动重启时**覆盖重新生成**，内容来自 `watchdog.py` 的 `WINDOWS_ADB_PATH` 等配置。

手动修改此文件是**临时有效**的，下次 watchdog 触发自动重启后会覆盖。只要 `watchdog.py` 里的默认值正确，自动重启生成的脚本就是正确的。

**注意（冷启动）**：`deploy_executor.sh` 假设 `start_executor.cmd` 已存在，自身不生成。全新机器初次部署时，需要先让 watchdog 跑一轮自动重启生成此文件，或手动创建一份。

---

## Watchdog 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MHXY_EXECUTOR_WATCHDOG_INTERVAL_SEC` | `60` | 每轮 health check 间隔 |
| `MHXY_EXECUTOR_WATCHDOG_HTTP_TIMEOUT_SEC` | `8` | 单次 HTTP 请求超时 |
| `MHXY_EXECUTOR_WATCHDOG_FAIL_THRESHOLD` | `3` | 连续失败几次触发 `restart_executor()` |
| `MHXY_EXECUTOR_WATCHDOG_RESTART_COOLDOWN_SEC` | `90` | **重启冷却期**：重启后 90s 内 health 失败不递增 fail count，防止 executor 启动慢（RapidOCR 加载 ~15s）触发二次 kill 循环 |
| `MHXY_EXECUTOR_WATCHDOG_APP_HEALTH_EVERY` | `5` | 每隔多少轮做一次 `/app_health` 深度检查 |

修改后需 `docker compose restart` watchdog 容器使其生效。

---

## 健康检查

```bash
curl http://192.168.100.149:8765/health        # 基础健康 + adb 路径确认
curl http://192.168.100.149:8765/list_devices  # ADB 可见实例列表
```
