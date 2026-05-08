# -*- coding: utf-8 -*-
"""LangChain tools for controlling Dream Journey Mobile via ADB."""
from __future__ import annotations

import base64
import json
import os
import random
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from openai import OpenAI

from mhxy_bot.config import (
    DEFAULT_RESOLUTION,
    ELEMENT_LIBRARY_JSON,
    INSTANCES_JSON,
    QWEN_VL_PLUS_MODEL,
)
from mhxy_bot.tools.executor_client import ExecutorClient


def _port_to_str(port: Any) -> str:
    port = str(port)
    return f"127.0.0.1:{port}" if ":" not in port else port


def _load_instances() -> dict:
    if not INSTANCES_JSON.exists():
        return {}
    return json.loads(INSTANCES_JSON.read_text(encoding="utf-8"))


def make_game_tools(sandbox_dir: Path, vl_registry=None) -> list:
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    ELEMENT_LIBRARY_JSON.parent.mkdir(parents=True, exist_ok=True)
    W, H = DEFAULT_RESOLUTION

    executor_url = os.getenv("MHXY_EXECUTOR_URL", "")
    if not executor_url:
        raise RuntimeError("MHXY_EXECUTOR_URL 未配置，请在 .env 中设置 Windows 执行器地址")
    executor = ExecutorClient(executor_url)

    def _clamp_normalized(x: float, y: float) -> tuple[float, float]:
        return max(0.0, min(float(x), 1.0)), max(0.0, min(float(y), 1.0))

    @tool
    def get_instances() -> str:
        """读取所有模拟器实例信息，包括端口、门派和队伍配置。"""
        try:
            data = _load_instances()
            if not data:
                return "❌ instances.json 不存在或为空，请先配置实例。"
            instances = data.get("instances", [])
            if not instances:
                return "❌ instances.json 中没有实例配置。"
            lines = [f"共 {len(instances)} 个模拟器实例："]
            for inst in instances:
                note = inst.get("note", "")
                note_str = f"  备注：{note}" if note else ""
                lines.append(f"  - 端口 {inst['port']}  门派：{inst.get('school', '未识别')}{note_str}")
            groups = data.get("groups", [])
            if groups:
                lines.append(f"\n【队伍配置】共 {len(groups)} 组：")
                for i, group in enumerate(groups, 1):
                    leader = group.get("leader", {})
                    lines.append(f"  第{i}组  队长：{leader.get('port')}（{leader.get('school', '?')}）")
                    for member in group.get("members", []):
                        lines.append(f"         队员：{member.get('port')}（{member.get('school', '?')}）")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 读取实例信息失败：{type(e).__name__} - {e}"

    @tool
    def check_instance_health(port: str = "") -> str:
        """诊断一个或多个模拟器实例的运行状态（ADB 连接 / 截图 / OCR / 游戏界面）。纯只读，不执行修复或重连。

        Args:
            port: 端口号（如 "5557"）、逗号分隔多端口（如 "5557,5559"），或留空诊断所有实例。

        Returns:
            单实例时返回完整诊断报告；多实例时返回汇总状态表。
        """
        try:
            from core.observability import attach_tool_meta
            from mhxy_bot.runner.context import RunnerContext
            from mhxy_bot.runner.instance_recovery import diagnose_instance

            if port.strip():
                port_list = [p.strip() for p in port.split(",") if p.strip()]
            else:
                port_list = [str(inst["port"]) for inst in _load_instances().get("instances", [])]

            if not port_list:
                return "❌ 没有可用实例"

            if len(port_list) == 1:
                port_str = _port_to_str(port_list[0])
                ctx = RunnerContext(executor=executor, port=port_str)
                diag = diagnose_instance(ctx)
                d = diag.as_dict()
                attach_tool_meta({
                    "kind": "instance_diagnosis",
                    "port": port_str,
                    "code": d["code"],
                    "state": d["state"],
                    "needs_human": d["needs_human"],
                    "steps": d["steps"],
                })
                if d["needs_human"]:
                    prefix = "⚠️ "
                elif d["code"] != "unknown_ok":
                    prefix = "❌ "
                else:
                    prefix = "✅ "
                steps_section = ""
                if diag.steps:
                    steps_text = "\n".join(f"  {s}" for s in diag.steps)
                    steps_section = f"\n【诊断过程】\n{steps_text}"
                return (
                    f"{prefix}端口 {port_list[0]} 健康诊断\n"
                    f"  状态：{d['state']}\n"
                    f"  问题码：{d['code']}\n"
                    f"  需要人工介入：{'是' if d['needs_human'] else '否'}\n"
                    f"  说明：{d['message']}"
                    f"{steps_section}"
                )

            ok = 0
            warn = 0
            lines = []
            batch_results = []
            for p in port_list:
                port_str = _port_to_str(p)
                try:
                    ctx = RunnerContext(executor=executor, port=port_str)
                    diag = diagnose_instance(ctx)
                    d = diag.as_dict()
                except Exception as e:
                    d = {"code": "error", "state": "error", "needs_human": True,
                         "message": str(e), "steps": []}
                if d["needs_human"] or d["code"] != "unknown_ok":
                    emoji = "⚠️"
                    warn += 1
                else:
                    emoji = "✅"
                    ok += 1
                tag = "[需恢复]" if (d["needs_human"] or d["code"] != "unknown_ok") else "[正常]"
                lines.append(f"  {emoji} 端口 {p}  {d['state']}  {d['code']}  {tag}")
                batch_results.append({
                    "port": port_str,
                    "code": d["code"],
                    "state": d["state"],
                    "needs_human": d["needs_human"],
                })
            attach_tool_meta({
                "kind": "instance_diagnosis_batch",
                "total": len(port_list),
                "ok": ok,
                "warn": warn,
                "results": batch_results,
            })
            return f"共检查 {len(port_list)} 个实例（{ok} 正常 / {warn} 需介入）：\n" + "\n".join(lines)

        except Exception as e:
            return f"❌ 诊断异常：{type(e).__name__} - {e}"

    @tool
    def reconnect_instances(ports: str = "") -> str:
        """主动对一个或多个模拟器实例执行重连恢复（处理掉线 / 登录界面 / 安卓桌面）。
        若实例当前不在可重连状态（如已在主界面），会被标记为 skipped。
        每个实例最多等待 60 秒，N 个实例最长耗时约 60×N 秒。

        Args:
            ports: 端口号（如 "5557"）、逗号分隔多端口（如 "5557,5559"），
                   或留空对所有实例执行重连。

        Returns:
            单实例时返回详细结果；多实例时返回汇总（成功 / 失败 / 跳过统计 + 各实例结果）。
        """
        try:
            from core.observability import attach_tool_meta
            from mhxy_bot.runner.context import RunnerContext
            from mhxy_bot.runner.instance_recovery import reconnect_one_port

            if ports.strip():
                port_list = [p.strip() for p in ports.split(",") if p.strip()]
            else:
                port_list = [str(inst["port"]) for inst in _load_instances().get("instances", [])]
            if not port_list:
                return "❌ 没有可用实例"

            # 软上限：留空且实例数 > 5 时拒绝无差别全量，避免 LLM 触发 60×N 秒阻塞
            BATCH_LIMIT = 5
            if not ports.strip() and len(port_list) > BATCH_LIMIT:
                return (
                    f"⚠️ 检测到 {len(port_list)} 个实例。无差别批量重连预计耗时 "
                    f">{60 * BATCH_LIMIT}s，建议先调 check_instance_health 筛出 "
                    f"`needs_human=True` 实例，再用 ports 参数显式指定端口。"
                )

            # 单端口：返回详细结果
            if len(port_list) == 1:
                port_str = _port_to_str(port_list[0])
                ctx = RunnerContext(executor=executor, port=port_str)
                outcome = reconnect_one_port(ctx, timeout_sec=60)
                attach_tool_meta({
                    "kind": "reconnect",
                    "port": port_str,
                    **outcome,
                })
                action = outcome["action"]
                if action == "skipped":
                    return (f"⏭️ 端口 {port_list[0]} 跳过重连\n"
                            f"  当前状态：{outcome['state']}（无需恢复）")
                prefix = "✅" if action == "reconnected" else "❌"
                verb = "成功" if action == "reconnected" else "失败"
                steps_section = ""
                if outcome.get("steps"):
                    steps_text = "\n".join(f"  {s}" for s in outcome["steps"])
                    steps_section = f"\n【恢复过程】\n{steps_text}"
                return (
                    f"{prefix} 端口 {port_list[0]} 重连{verb}\n"
                    f"  初始状态：{outcome['initial_state']}\n"
                    f"  最终状态：{outcome['final_state']}"
                    f"{steps_section}"
                )

            # 多端口：顺序执行 + 汇总
            ok = bad = skip = 0
            lines = []
            batch_results = []
            for p in port_list:
                port_str = _port_to_str(p)
                try:
                    ctx = RunnerContext(executor=executor, port=port_str)
                    outcome = reconnect_one_port(ctx, timeout_sec=60)
                except Exception as exc:
                    outcome = {"action": "failed", "initial_state": "error",
                               "final_state": f"error: {type(exc).__name__}",
                               "steps": []}
                action = outcome["action"]
                if action == "reconnected":
                    emoji, detail = "✅", outcome["final_state"]
                    ok += 1
                elif action == "failed":
                    emoji, detail = "❌", outcome["final_state"]
                    bad += 1
                else:  # skipped
                    emoji, detail = "⏭️", outcome["state"]
                    skip += 1
                lines.append(f"  {emoji} 端口 {p}  {detail}")
                batch_results.append({"port": port_str, **outcome})

            attach_tool_meta({
                "kind": "reconnect",
                "batch_size": len(port_list),
                "ok": ok,
                "failed": bad,
                "skipped": skip,
                "results": batch_results,
            })
            return (f"共重连 {len(port_list)} 个实例"
                    f"（{ok} 成功 / {bad} 失败 / {skip} 跳过）：\n"
                    + "\n".join(lines))
        except Exception as e:
            return f"❌ 重连异常：{type(e).__name__} - {e}"

    @tool
    def capture_screenshot(port: str) -> str:
        """对指定端口截图并返回图片给 Telegram。port 如 5557 或 127.0.0.1:5557。"""
        try:
            img_b64 = executor.screenshot(port)
            path = sandbox_dir / f"screenshot_{str(port).replace(':', '_')}_{int(time.time())}.png"
            path.write_bytes(base64.b64decode(img_b64))
            return f"✅ 已截图端口 {port}\n[IMG:{path}]"
        except Exception as e:
            return f"❌ 截图失败：{type(e).__name__} - {e}"

    @tool
    def sense_screen(port: str) -> str:
        """对指定端口截图并 OCR 识别，返回文字及坐标。"""
        try:
            results = executor.sense(port)
            if not results:
                return "屏幕未识别到任何文字。"
            lines = [f"识别到 {len(results)} 条文字："]
            for item in results:
                x, y = _clamp_normalized(item["center_x"] / W, item["center_y"] / H)
                lines.append(
                    f"  像素[{item['center_x']:.0f}, {item['center_y']:.0f}] "
                    f"归一化({x:.3f}, {y:.3f}) "
                    f"'{item['text']}'  (置信度 {item['confidence']:.2f})"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 屏幕感知失败：{type(e).__name__} - {e}"

    @tool
    def tap_coordinate(port: str, x: float, y: float) -> str:
        """点击指定模拟器的归一化坐标（0-1）。"""
        try:
            px = max(0, min(int(x * W + random.randint(-5, 5)), W - 1))
            py = max(0, min(int(y * H + random.randint(-5, 5)), H - 1))
            ok = executor.tap(port, px, py)
            return f"✅ 已点击坐标 ({px}, {py})（原始归一化：{x:.3f}, {y:.3f}）" if ok else "❌ ADB 点击命令执行失败"
        except Exception as e:
            return f"❌ 点击失败：{type(e).__name__} - {e}"

    @tool
    def batch_tap_coordinate(x: float, y: float, ports: str = "") -> str:
        """批量点击所有实例或指定实例的同一归一化坐标。"""
        try:
            if ports.strip():
                port_list = [p.strip() for p in ports.split(",") if p.strip()]
            else:
                port_list = [str(inst["port"]) for inst in _load_instances().get("instances", [])]
            if not port_list:
                return "❌ 没有可用实例"
            px = max(0, min(int(x * W) + random.randint(-5, 5), W - 1))
            py = max(0, min(int(y * H) + random.randint(-5, 5), H - 1))
            results_map = executor.batch_tap(port_list, px, py)
            lines = [f"  {'✅' if ok else '❌'} {port}" for port, ok in results_map.items()]
            return f"批量点击 ({x:.3f}, {y:.3f}) — {len(port_list)} 个实例：\n" + "\n".join(lines)
        except Exception as e:
            return f"❌ 批量点击失败：{type(e).__name__} - {e}"

    @tool
    def tap_saved_element(port: str, element_name: str) -> str:
        """从元素库读取已保存元素坐标并直接点击。"""
        try:
            from mhxy_bot.game_core.element_library import get_element_library

            lib = get_element_library()
            element = lib.get_element(element_name)
            if not element:
                return f"❌ 元素库中未找到元素「{element_name}」"
            x = element.get("x")
            y = element.get("y")
            if x is None or y is None:
                return f"❌ 元素「{element_name}」缺少可点击坐标"
            return tap_coordinate.invoke({"port": port, "x": float(x), "y": float(y)})
        except Exception as e:
            return f"❌ 从元素库点击失败：{type(e).__name__} - {e}"

    @tool
    def press_back(port: str) -> str:
        """按下指定模拟器返回键。"""
        try:
            ok = executor.back(port)
            return "✅ 已按返回键" if ok else "❌ ADB 返回键命令执行失败"
        except Exception as e:
            return f"❌ 返回键失败：{type(e).__name__} - {e}"

    @tool
    def batch_press_back(ports: str = "") -> str:
        """批量对所有实例或指定实例按返回键。"""
        try:
            if ports.strip():
                port_list = [p.strip() for p in ports.split(",") if p.strip()]
            else:
                port_list = [str(inst["port"]) for inst in _load_instances().get("instances", [])]
            if not port_list:
                return "❌ 没有可用实例"
            results_map = executor.batch_back(port_list)
            lines = [f"  {'✅' if ok else '❌'} {port}" for port, ok in results_map.items()]
            return f"批量返回 — {len(port_list)} 个实例：\n" + "\n".join(lines)
        except Exception as e:
            return f"❌ 批量返回失败：{type(e).__name__} - {e}"

    @tool
    def analyze_scene(port: str, prompt: str = "") -> str:
        """用 Qwen-VL 分析指定模拟器当前屏幕的游戏场景。"""
        try:
            from mhxy_bot.game_core.cloud_vision import _log_vl_call

            img_b64 = executor.screenshot(port)
            user_prompt = prompt or """请详细分析这张梦幻西游手游的截图，包括：
1. 游戏场景（在哪里）
2. 角色信息（等级、门派、外观）
3. 界面元素（打开了哪些功能按钮）
4. 任务信息（当前有什么任务）
5. 其他重要信息（活动、聊天等）"""
            client = OpenAI(
                api_key=os.getenv("VL_DASHSCOPE_API_KEY") or os.getenv("DASHSCOPE_API_KEY", ""),
                base_url=os.getenv("VL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            )
            vl_model = vl_registry.current_model() if vl_registry else QWEN_VL_PLUS_MODEL
            start = time.perf_counter()
            resp = client.chat.completions.create(
                model=vl_model,
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    {"type": "text", "text": user_prompt},
                ]}],
                max_tokens=1024,
            )
            duration_ms = (time.perf_counter() - start) * 1000
            content = resp.choices[0].message.content or ""
            usage = {
                "input_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "output_tokens": getattr(resp.usage, "completion_tokens", 0),
                "total_tokens": getattr(resp.usage, "total_tokens", 0),
            }
            _log_vl_call(vl_model, duration_ms, success=True, usage=usage, prompt=user_prompt, raw_output=content)
            return f"🔍 场景分析结果：\n{content}"
        except Exception as e:
            return f"❌ 场景分析异常：{type(e).__name__} - {e}"

    @tool
    def locate_element_vl(port: str, element_name: str) -> str:
        """用 Qwen-VL 定位指定 UI 元素的归一化坐标。"""
        try:
            from mhxy_bot.game_core.cloud_vision import CloudVisionAnalyzer

            img_b64 = executor.screenshot(port)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                temp_path = Path(f.name)
            temp_path.write_bytes(base64.b64decode(img_b64))
            try:
                result = CloudVisionAnalyzer(
                    model=vl_registry.current_model() if vl_registry else None
                ).locate_element(str(temp_path), target=element_name)
            finally:
                temp_path.unlink(missing_ok=True)
            if not result.get("success"):
                return f"❌ VL 识别失败：{result.get('error')}"
            elements = result.get("elements", [])
            if not elements:
                return f"未找到元素「{element_name}」"
            elem = elements[0]
            x, y = elem.get("x", 0), elem.get("y", 0)
            desc = elem.get("description", "")
            return f"找到元素「{element_name}」：坐标 ({x:.3f}, {y:.3f})" + (f"  {desc}" if desc else "")
        except Exception as e:
            return f"❌ VL 元素识别异常：{type(e).__name__} - {e}"

    @tool
    def list_element_library() -> str:
        """列出 UI 元素库中所有已保存元素名称和坐标。"""
        try:
            from mhxy_bot.game_core.element_library import get_element_library

            lib = get_element_library()
            elements = lib.get_all_elements()
            if not elements:
                return "元素库为空，可用 locate_element_vl 识别后保存。"
            lines = [f"元素库共 {lib.count()} 个元素："]
            for name, info in elements.items():
                lines.append(f"  · {name}  ({float(info.get('x', 0)):.3f}, {float(info.get('y', 0)):.3f})  {info.get('description', '')}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 读取元素库失败：{type(e).__name__} - {e}"

    @tool
    def save_to_element_library(element_name: str, x: float, y: float, description: str = "") -> str:
        """将 UI 元素归一化坐标保存到持久化元素库。"""
        try:
            from mhxy_bot.game_core.element_library import get_element_library

            lib = get_element_library()
            x, y = _clamp_normalized(x, y)
            lib.add_element(element_name, x, y, description=description)
            lib.save_to_file()
            return f"✅ 已保存元素 '{element_name}' ({x:.3f}, {y:.3f}) 到元素库"
        except Exception as e:
            return f"❌ 保存失败：{type(e).__name__} - {e}"

    @tool
    def delete_from_element_library(element_name: str) -> str:
        """从元素库删除指定元素。"""
        try:
            from mhxy_bot.game_core.element_library import get_element_library

            lib = get_element_library()
            if element_name not in lib.elements:
                return f"❌ 元素库中不存在 '{element_name}'"
            lib.remove_element(element_name)
            lib.save_to_file()
            return f"✅ 已从元素库删除 '{element_name}'"
        except Exception as e:
            return f"❌ 删除失败：{type(e).__name__} - {e}"

    @tool
    def batch_recognize_schools() -> str:
        """批量识别所有实例门派并更新 instances.json。"""
        try:
            data = _load_instances()
            instances = data.get("instances", [])
            if not instances:
                return "❌ instances.json 中没有实例，请先配置端口"
            school_names = [
                "大唐官府", "方寸山", "化生寺", "女儿村", "须弥海",
                "月宫", "龙宫", "普陀山", "花果山",
                "阴曹地府", "魔王寨", "狮驼岭", "小雷音", "盘丝洞",
            ]
            aliases = {
                "大唐": "大唐官府", "方寸": "方寸山", "化生": "化生寺",
                "女儿": "女儿村", "须弥": "须弥海", "普陀": "普陀山",
                "花果": "花果山", "地府": "阴曹地府", "魔王": "魔王寨",
                "狮驼": "狮驼岭", "雷音": "小雷音", "盘丝": "盘丝洞",
            }

            def match_school(text: str) -> str | None:
                for name in school_names:
                    if name in text or name == text:
                        return name
                for alias, full in aliases.items():
                    if alias in text or alias == text:
                        return full
                return None

            results = []
            for inst in instances:
                port = str(inst.get("port"))
                try:
                    ocr_text = sense_screen.invoke({"port": port})
                    school = None
                    for line in ocr_text.splitlines():
                        school = match_school(line)
                        if school:
                            break
                    if school:
                        inst["school"] = school
                        results.append(f"  ✅ 端口 {port} → {school}")
                    else:
                        results.append(f"  ❌ 端口 {port} → 识别失败")
                except Exception as e:
                    results.append(f"  ⚠️ 端口 {port} → 异常：{type(e).__name__}")
                time.sleep(random.uniform(0.5, 1.0))
            data["scan_time"] = datetime.now().isoformat()
            INSTANCES_JSON.parent.mkdir(parents=True, exist_ok=True)
            INSTANCES_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return "批量识别完成：\n" + "\n".join(results)
        except Exception as e:
            return f"❌ 批量识别失败：{type(e).__name__} - {e}"

    return [
        get_instances,
        batch_recognize_schools,
        check_instance_health,
        reconnect_instances,
        capture_screenshot,
        sense_screen,
        analyze_scene,
        locate_element_vl,
        tap_coordinate,
        batch_tap_coordinate,
        tap_saved_element,
        press_back,
        batch_press_back,
        list_element_library,
        save_to_element_library,
        delete_from_element_library,
    ]
