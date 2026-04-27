# -*- coding: utf-8 -*-
"""
Actuator - ADB 执行层

提供 ADB 点击、滑动等操作
"""

import subprocess

from mhxy_bot.config import ADB_PATH, REMOTE_MODE, REMOTE_HOST, REMOTE_USER


class GameActuator:
    """
    游戏执行器：ADB tap / swipe / back / home
    """

    def __init__(self, port: str, adb_path: str = None, log_level: str = "INFO"):
        """
        初始化执行器

        Args:
            port: 模拟器端口
            adb_path: ADB 路径
            log_level: 日志级别
        """
        self.port = port
        self.adb_path = adb_path or ADB_PATH
        self.log_level = log_level

    def _adb_serial(self) -> str:
        """返回 ADB serial，纯数字端口补全为 127.0.0.1:{port}"""
        port = str(self.port)
        return f"127.0.0.1:{port}" if port.isdigit() else port

    def _run_adb(self, shell_cmd: str) -> bool:
        """
        执行 ADB 命令

        远程模式下通过 SSH 转发到 Windows 执行。

        Args:
            shell_cmd: Shell 命令

        Returns:
            是否成功
        """
        serial = self._adb_serial()
        connect_cmd = f'"{self.adb_path}" connect {serial}'
        adb_cmd = f'"{self.adb_path}" -s {serial} shell {shell_cmd}'

        if REMOTE_MODE:
            combined = f'{connect_cmd} >nul 2>&1 & {adb_cmd}'
            result = subprocess.run(
                ['ssh', f'{REMOTE_USER}@{REMOTE_HOST}', combined],
                capture_output=True, timeout=10
            )
        else:
            subprocess.run(connect_cmd, shell=True, capture_output=True, timeout=10)
            result = subprocess.run(adb_cmd, shell=True, capture_output=True, timeout=10)

        return result.returncode == 0

    def tap(self, x: int, y: int) -> bool:
        """
        点击

        Args:
            x: X 坐标
            y: Y 坐标

        Returns:
            是否成功
        """
        return self._run_adb(f"input tap {x} {y}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 500) -> bool:
        """
        滑动

        Args:
            x1, y1: 起始坐标
            x2, y2: 结束坐标
            duration: 滑动时长（毫秒）

        Returns:
            是否成功
        """
        return self._run_adb(f"input swipe {x1} {y1} {x2} {y2} {duration}")

    def back(self) -> bool:
        """
        返回键

        Returns:
            是否成功
        """
        return self._run_adb("input keyevent KEYCODE_BACK")

    def home(self) -> bool:
        """
        Home 键

        Returns:
            是否成功
        """
        return self._run_adb("input keyevent KEYCODE_HOME")

    def close(self):
        """释放资源（无特殊清理，保留此方法用于 API 一致性）"""
        pass
