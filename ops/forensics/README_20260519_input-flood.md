# 取证：画面正常但点不动（输入事件洪流积压）— 2026-05-19 16:09 CST

## 一句话结论

MuMu 虚拟手柄 `Xiaomi Joystick`（deviceId=4）幽灵持键 + 摇杆轴卡死，以 ~260/s 灌爆
InputDispatcher 入站队列；队列恒定 ~2600 深、每事件积压 ~10s，撞 AOSP
`STALE_EVENT_TIMEOUT(10s)` 全部丢弃 → 真实点击（obs tap + 手动鼠标）一并被丢。
**7 个实例同时中招**。InputDispatcher / system_server / 游戏 app 均健康（非挂死、非 ANR）。

## 证据文件

| 文件 | 内容 | 关键信号 |
|---|---|---|
| `anr_5556_logcat_20260519_160908.txt` | emulator-5556 logcat -d -t 4000 | 14s 内 `Dropped event because it is stale.` ×3728；`LatencyTracker latency: ~10120ms` |
| `dumpsys_input_5556_20260519_160908.txt` | emulator-5556 dumpsys input（**未卡，正常返回**） | `InboundQueue: length=2578`，每条 `age≈10048ms`；`DispatchEnabled:true DispatchFrozen:false`；`Device 4 Xiaomi Joystick … KeyDowns: 3 keys currently down` |

## 排他验证

- `/list_devices` count=7 → 排除 stale daemon
- HTTP `/tap` 5557 → 卡 15.09s 超时（注入事件排队尾等不到 finish）
- `/data/anr/` 空 → 非 ANR
- 7 实例 InboundQueue 全 ~2550–2670 → 系统性、同时发生（疑 MuMu 多开键鼠/手柄同步把 key-down 锁住）

## 旧手册误判（已在 windows_executor.md 订正）

- ❌ "system_server / InputDispatcher 挂死" → 实为分发器健康、被灌爆
- ❌ "dumpsys input 会一并卡住" → 实为正常返回，证据就在其输出
- ❌ "疑似 screenrecord H264 拖垮 guest" → 与本故障无关，真因是虚拟手柄幽灵持键

## 复发对比

复发时重抓 `dumpsys input` 看 `InboundQueue: length` 与 `Xiaomi Joystick KeyDowns`，
与本目录样本一致即同因，直接重启 MuMu 全部实例。
