# -*- coding: utf-8 -*-
"""
Sensor - 感知层：截图 + OCR
"""

import subprocess
import numpy as np
import cv2
from typing import List, Dict, Any

from mhxy_bot.config import ADB_PATH, REMOTE_MODE, REMOTE_HOST, REMOTE_USER


class GameSensor:
    """
    游戏传感器：screenshot() + sense()（OCR）
    """

    def __init__(self, port: str, adb_path: str = None, log_level: str = "INFO"):
        """
        初始化传感器

        Args:
            port: 模拟器端口
            adb_path: ADB 路径
            log_level: 日志级别
        """
        self.port = port
        self.adb_path = adb_path or ADB_PATH
        self.log_level = log_level

        # 延迟初始化 OCR
        self._ocr = None

    def _get_ocr(self):
        """获取 OCR 实例（延迟初始化）"""
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            self._ocr = RapidOCR()
        return self._ocr

    def _adb_serial(self) -> str:
        """返回 ADB serial，纯数字端口补全为 127.0.0.1:{port}"""
        port = str(self.port)
        return f"127.0.0.1:{port}" if port.isdigit() else port

    def screenshot(self) -> Any:
        """
        截图

        通过 adb exec-out screencap -p 将 PNG 直接管道传输到内存，
        不在模拟器或本地磁盘产生临时文件。
        远程模式下经由 SSH 回传到 Mac。

        Returns:
            OpenCV 图像
        """
        serial = self._adb_serial()
        connect_cmd = f'"{self.adb_path}" connect {serial}'
        adb_cmd = f'"{self.adb_path}" -s {serial} exec-out screencap -p'

        if REMOTE_MODE:
            combined = f'{connect_cmd} >nul 2>&1 & {adb_cmd}'
            result = subprocess.run(['ssh', f'{REMOTE_USER}@{REMOTE_HOST}', combined],
                                    capture_output=True, timeout=15)
        else:
            subprocess.run(connect_cmd, shell=True, capture_output=True, timeout=10)
            result = subprocess.run(adb_cmd, shell=True, capture_output=True, timeout=15)

        image = cv2.imdecode(np.frombuffer(result.stdout, np.uint8), cv2.IMREAD_COLOR)
        return image

    def sense(self) -> List[Dict[str, Any]]:
        """
        感知（截图 + OCR）

        Returns:
            识别结果列表：[{"text": "文字", "center_x": x, "center_y": y, "confidence": 置信度}, ...]
        """
        # 截图
        image = self.screenshot()

        # OCR 识别
        ocr = self._get_ocr()
        result, _ = ocr(image)

        # 解析结果，RapidOCR 格式：[[bbox, text, confidence], ...]
        texts = []
        if result:
            for bbox, text, confidence in result:
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
                center_x = int(sum(xs) / len(xs))
                center_y = int(sum(ys) / len(ys))

                texts.append({
                    "text": text,
                    "center_x": center_x,
                    "center_y": center_y,
                    "confidence": float(confidence)
                })

        return texts

    def close(self):
        """释放资源（OCR 实例不需要特殊清理，保留此方法用于 API 一致性）"""
        self._ocr = None
