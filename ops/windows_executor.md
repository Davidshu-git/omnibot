# Windows Executor 运维手册

Windows executor 运行在 `192.168.100.149`，HTTP 端口 `8765`。
代码目录：`C:\Users\sdw\mhxy_executor\`，启动脚本：`start_executor.cmd`。

---

## 部署 / 更新代码

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

# 第 3 步：用 cmd.exe 后台启动，不能直接调 .cmd 文件
ssh -i /home/shudawei/.ssh/id_towin -o StrictHostKeyChecking=no \
  sdw@192.168.100.149 \
  "powershell -NoProfile -Command \"Start-Process cmd.exe -ArgumentList '/c C:\\Users\\sdw\\mhxy_executor\\start_executor.cmd' -WindowStyle Hidden\""

# 验证
sleep 6
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
