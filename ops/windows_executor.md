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
4. `cscript //NoLogo //B launch_executor.vbs`（VBScript 完全脱离 SSH 会话后台拉起）
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

# 第 3 步：通过 VBScript 后台启动（推荐，比 Start-Process 可靠）
ssh -i /home/shudawei/.ssh/id_towin -o StrictHostKeyChecking=no \
  sdw@192.168.100.149 \
  "cscript //NoLogo //B C:\\Users\\sdw\\mhxy_executor\\launch_executor.vbs"

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

### 坑 4：Start-Process 失败时无任何报错，需靠日志确认（2026-05-12 实测）

**现象**：按三步流程重启，`Start-Process` 返回无输出（正常），但 `tasklist | findstr python` 无进程，`curl /health` 超时。原因不明，复现两次后消失——历史记录显示同样的 Start-Process 方式在其他时间点（PID 26248、10040）均正常工作，判断为偶发性故障。

**关键教训**：`Start-Process` 失败时不会抛出任何错误，`tasklist` 也可能因进程瞬间退出而看不到。**唯一可靠的验证方式是检查日志**：

```bash
ssh ... "type C:\\Users\\sdw\\mhxy_executor\\stderr-watchdog.log" | tail -5
```

重启成功时日志末尾应出现：
```
INFO:     Started server process [XXXXX]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8765
```

如果日志没有新增内容，说明进程在写第一行日志之前就退出了，可能原因：端口未释放、瞬态状态。此时等待 5–10s 再重试一次。

**补充：EADDRINUSE 错误**：若日志出现 `[Errno 10048] ... 端口只允许使用一次`，说明 8765 已被占用，需先确认旧进程已彻底退出再重启：

```bash
ssh ... "powershell -NoProfile -Command \"netstat -ano | findstr :8765\""
```

无输出才可以启动新进程。

---

### 坑 5：不能 `taskkill /IM cmd.exe /F`（2026-05-12 实测）

**现象**：deploy 脚本同时杀 `python.exe` 和 `cmd.exe` 后，VBScript 启动的新进程进入 cmd.exe 但 python 进程始终不出现，log 文件也无新写入。

**原因**：`start_executor.cmd` 使用 `>> stderr-watchdog.log` 追加重定向。强杀 cmd.exe（`/F`）导致该文件的 OS 写句柄未经正常 flush/close 直接释放。后续新 cmd.exe 运行同一 batch 文件时，`>>` 追加操作失败（写入目标处于不一致状态），cmd 以非零退出但不输出任何错误，整个进程静默消失。

**正确做法**：只杀 `python.exe`。start_executor.cmd 的父 cmd.exe 会在 python 退出后自然退出，log 文件句柄得到正常释放。

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

---

## 健康检查

```bash
curl http://192.168.100.149:8765/health        # 基础健康 + adb 路径确认
curl http://192.168.100.149:8765/list_devices  # ADB 可见实例列表
```
