# -*- coding: utf-8 -*-
"""LangChain tools for controlling Dream Journey Mobile via ADB."""
from __future__ import annotations

import base64
import json
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


def _port_to_str(port: Any) -> str:
    port = str(port)
    return f"127.0.0.1:{port}" if ":" not in port else port


def _load_instances() -> dict:
    if not INSTANCES_JSON.exists():
        return {}
    return json.loads(INSTANCES_JSON.read_text(encoding="utf-8"))


def make_game_tools(sandbox_dir: Path) -> list:
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    ELEMENT_LIBRARY_JSON.parent.mkdir(parents=True, exist_ok=True)
    W, H = DEFAULT_RESOLUTION

    def _clamp_normalized(x: float, y: float) -> tuple[float, float]:
        return max(0.0, min(float(x), 1.0)), max(0.0, min(float(y), 1.0))

    @tool
    def get_instances() -> str:
        """读取所有模拟器实例信息，包括端口、门派和队伍配置。"""
        try:
            data = _load_instances()
            if not data:
                return "❌ instances.json 不存在或为空，请先配置实例。"
            lines = [f"扫描时间：{data.get('scan_time', '未知')}"]
            instances = data.get("instances", [])
            lines.append(f"\n【实例列表】共 {len(instances)} 个：")
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
    def capture_screenshot(port: str) -> str:
        """对指定端口截图并返回图片给 Telegram。port 如 5557 或 127.0.0.1:5557。"""
        try:
            import cv2
            from mhxy_bot.game_core.sensor import GameSensor

            sensor = GameSensor(port=_port_to_str(port))
            img = sensor.screenshot()
            sensor.close()
            if img is None:
                return "❌ 截图失败：ADB 未返回有效图像"
            path = sandbox_dir / f"screenshot_{str(port).replace(':', '_')}_{int(time.time())}.png"
            cv2.imwrite(str(path), img)
            return f"✅ 已截图端口 {port}\n[IMG:{path}]"
        except Exception as e:
            return f"❌ 截图失败：{type(e).__name__} - {e}"

    @tool
    def sense_screen(port: str) -> str:
        """对指定端口截图并 OCR 识别，返回文字及坐标。"""
        try:
            from mhxy_bot.game_core.sensor import GameSensor

            sensor = GameSensor(port=_port_to_str(port))
            results = sensor.sense()
            sensor.close()
            if not results:
                return "屏幕未识别到任何文字。"
            lines = [f"识别到 {len(results)} 条文字："]
            for item in results:
                x, y = _clamp_normalized(item["center_x"] / W, item["center_y"] / H)
                lines.append(
                    f"  像素[{item['center_x']}, {item['center_y']}] "
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
            from mhxy_bot.game_core.actuator import GameActuator

            px = max(0, min(int(x * W + random.randint(-5, 5)), W - 1))
            py = max(0, min(int(y * H + random.randint(-5, 5)), H - 1))
            actuator = GameActuator(port=_port_to_str(port))
            ok = actuator.tap(px, py)
            actuator.close()
            time.sleep(random.uniform(0.2, 0.5))
            return f"✅ 已点击坐标 ({px}, {py})（原始归一化：{x:.3f}, {y:.3f}）" if ok else "❌ ADB 点击命令执行失败"
        except Exception as e:
            return f"❌ 点击失败：{type(e).__name__} - {e}"

    @tool
    def batch_tap_coordinate(x: float, y: float, ports: str = "") -> str:
        """批量点击所有实例或指定实例的同一归一化坐标。"""
        try:
            from mhxy_bot.game_core.actuator import GameActuator

            if ports.strip():
                port_list = [p.strip() for p in ports.split(",") if p.strip()]
            else:
                port_list = [str(inst["port"]) for inst in _load_instances().get("instances", [])]
            if not port_list:
                return "❌ 没有可用实例"
            results = []
            for port in port_list:
                try:
                    px = max(0, min(int(x * W) + random.randint(-5, 5), W - 1))
                    py = max(0, min(int(y * H) + random.randint(-5, 5), H - 1))
                    act = GameActuator(port=_port_to_str(port))
                    ok = act.tap(px, py)
                    act.close()
                    results.append(f"  {'✅' if ok else '❌'} {port}")
                    time.sleep(random.uniform(0.3, 0.7))
                except Exception as e:
                    results.append(f"  ❌ {port}：{type(e).__name__} - {e}")
            return f"批量点击 ({x:.3f}, {y:.3f}) — {len(port_list)} 个实例：\n" + "\n".join(results)
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
            from mhxy_bot.game_core.actuator import GameActuator

            actuator = GameActuator(port=_port_to_str(port))
            ok = actuator.back()
            actuator.close()
            time.sleep(0.3)
            return "✅ 已按返回键" if ok else "❌ ADB 返回键命令执行失败"
        except Exception as e:
            return f"❌ 返回键失败：{type(e).__name__} - {e}"

    @tool
    def batch_press_back(ports: str = "") -> str:
        """批量对所有实例或指定实例按返回键。"""
        try:
            from mhxy_bot.game_core.actuator import GameActuator

            if ports.strip():
                port_list = [p.strip() for p in ports.split(",") if p.strip()]
            else:
                port_list = [str(inst["port"]) for inst in _load_instances().get("instances", [])]
            if not port_list:
                return "❌ 没有可用实例"
            results = []
            for port in port_list:
                try:
                    act = GameActuator(port=_port_to_str(port))
                    ok = act.back()
                    act.close()
                    results.append(f"  {'✅' if ok else '❌'} {port}")
                    time.sleep(0.3)
                except Exception as e:
                    results.append(f"  ❌ {port}：{type(e).__name__} - {e}")
            return f"批量返回 — {len(port_list)} 个实例：\n" + "\n".join(results)
        except Exception as e:
            return f"❌ 批量返回失败：{type(e).__name__} - {e}"

    @tool
    def analyze_scene(port: str, prompt: str = "") -> str:
        """用 Qwen-VL 分析指定模拟器当前屏幕的游戏场景。"""
        try:
            import cv2
            from mhxy_bot.game_core.cloud_vision import _log_vl_call
            from mhxy_bot.game_core.sensor import GameSensor
            import os

            sensor = GameSensor(port=_port_to_str(port))
            img = sensor.screenshot()
            sensor.close()
            if img is None:
                return "❌ 场景分析失败：ADB 未返回有效图像"
            _, buf = cv2.imencode(".png", img)
            b64 = base64.b64encode(buf.tobytes()).decode()
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
            start = time.perf_counter()
            resp = client.chat.completions.create(
                model=QWEN_VL_PLUS_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
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
            _log_vl_call(QWEN_VL_PLUS_MODEL, duration_ms, success=True, usage=usage, prompt=user_prompt, raw_output=content)
            return f"🔍 场景分析结果：\n{content}"
        except Exception as e:
            return f"❌ 场景分析异常：{type(e).__name__} - {e}"

    @tool
    def locate_element_vl(port: str, element_name: str) -> str:
        """用 Qwen-VL 定位指定 UI 元素的归一化坐标。"""
        try:
            import cv2
            from mhxy_bot.game_core.cloud_vision import CloudVisionAnalyzer
            from mhxy_bot.game_core.sensor import GameSensor

            sensor = GameSensor(port=_port_to_str(port))
            img = sensor.screenshot()
            sensor.close()
            if img is None:
                return "❌ VL 元素识别失败：ADB 未返回有效图像"
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                temp_path = Path(f.name)
            cv2.imwrite(str(temp_path), img)
            try:
                result = CloudVisionAnalyzer().locate_element(str(temp_path), target=element_name)
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
