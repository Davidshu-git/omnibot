# -*- coding: utf-8 -*-
"""测试 diagnose_instance 的 steps 累加逻辑。"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from mhxy_bot.runner.context import RunnerContext
from mhxy_bot.runner.instance_recovery import diagnose_instance
from mhxy_bot.runner.models import InstanceIssue, InstanceState
from mhxy_bot.tools.executor_client import ExecutorClient


def _make_ctx(executor: ExecutorClient | None = None) -> RunnerContext:
    return RunnerContext(
        executor=executor or MagicMock(spec=ExecutorClient),
        port="5557",
    )


def test_adb_offline_adds_step():
    """ADB 离线时 steps 应包含 "ADB 未连接"。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": False}

    ctx = _make_ctx(executor)
    diag = diagnose_instance(ctx)

    assert diag.steps, "steps 不应为空"
    assert any("ADB 未连接" in s for s in diag.steps)
    assert diag.code == InstanceIssue.ADB_OFFLINE


def test_ocr_exception_adds_step():
    """OCR sense 抛出异常时 steps 应包含 "OCR sense 调用失败"。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}
    executor.sense.side_effect = Exception("OCR timeout")

    ctx = _make_ctx(executor)
    diag = diagnose_instance(ctx)

    assert any("OCR sense 调用失败" in s for s in diag.steps)
    assert diag.code == InstanceIssue.OCR_FAILED


def test_normal_path_steps_no_message_duplicate():
    """正常路径 steps 应包含屏幕状态识别，且不含 message 同义重复。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}
    executor.sense.return_value = [{"text": "test", "center_x": 100, "center_y": 200, "confidence": 0.9}]

    with patch(
        "mhxy_bot.runner.perception.detect_screen_state",
        return_value=InstanceState.MAIN_UI,
    ):
        ctx = _make_ctx(executor)
        diag = diagnose_instance(ctx)

    assert any("屏幕状态识别：main_ui" in s for s in diag.steps)
    # steps 不应与 message 字段同义重复
    assert not any("instance usable" in s.lower() for s in diag.steps)
    assert diag.code == InstanceIssue.UNKNOWN_OK
    assert diag.needs_human is False


def test_internal_exception_uses_unknown_ok_code():
    """detect_screen_state 内部抛出意外异常时，兜底返回 UNKNOWN_OK（而非 ADB_OFFLINE）。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}
    executor.sense.return_value = []

    with patch(
        "mhxy_bot.runner.perception.detect_screen_state",
        side_effect=RuntimeError("unexpected internal boom"),
    ):
        ctx = _make_ctx(executor)
        diag = diagnose_instance(ctx)

    assert diag.code == InstanceIssue.UNKNOWN_OK, "兜底异常应使用 UNKNOWN_OK，不得误报 ADB_OFFLINE"
    assert diag.needs_human is True
    assert diag.state == InstanceState.UNKNOWN
    assert any("诊断内部异常" in s for s in diag.steps)


def test_try_reconnect_success_writes_step():
    """try_reconnect 成功时应向 steps 写入"自动恢复成功"。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}
    executor.sense.return_value = []

    call_count = 0

    def _detect_side_effect(ctx):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return InstanceState.DISCONNECTED
        return InstanceState.MAIN_UI

    with patch("mhxy_bot.runner.perception.detect_screen_state", side_effect=_detect_side_effect), \
         patch("mhxy_bot.runner.instance_recovery.try_reconnect") as mock_reconnect:

        def _reconnect_with_step(ctx, timeout_sec=90, steps=None):
            if steps is not None:
                steps.append("自动恢复成功，回到主界面")
            return True

        mock_reconnect.side_effect = _reconnect_with_step
        ctx = _make_ctx(executor)
        diag = diagnose_instance(ctx)

    assert any("自动恢复成功" in s for s in diag.steps)
    assert diag.code == InstanceIssue.UNKNOWN_OK
    assert diag.needs_human is False


def test_try_reconnect_timeout_writes_step():
    """try_reconnect 超时时应向 steps 写入"自动恢复失败或超时"。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}
    executor.sense.return_value = []

    with patch("mhxy_bot.runner.perception.detect_screen_state", return_value=InstanceState.DISCONNECTED), \
         patch("mhxy_bot.runner.instance_recovery.try_reconnect") as mock_reconnect:

        def _reconnect_timeout(ctx, timeout_sec=90, steps=None):
            if steps is not None:
                steps.append(f"自动恢复失败或超时（{timeout_sec}s）")
            return False

        mock_reconnect.side_effect = _reconnect_timeout
        ctx = _make_ctx(executor)
        diag = diagnose_instance(ctx)

    assert any("自动恢复失败或超时" in s for s in diag.steps)
    assert diag.code == InstanceIssue.DISCONNECTED
    assert diag.needs_human is True


def test_steps_capped_at_12():
    """steps 超出 12 条时应截断并追加"…（已截断）"。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}
    # sense 每次返回会触发多次 _cap_steps 调用，通过 detect_screen_state mock 控制循环次数
    executor.sense.return_value = []

    call_count = 0

    def _detect_many_states(ctx):
        nonlocal call_count
        call_count += 1
        # 前 11 次返回 DISCONNECTED，触发 reconnect，最终返回 MAIN_UI
        return InstanceState.MAIN_UI

    with patch("mhxy_bot.runner.perception.detect_screen_state", side_effect=_detect_many_states), \
         patch("mhxy_bot.runner.instance_recovery.try_reconnect") as mock_reconnect:

        # 注入 13 条 steps（超限场景）
        def _inject_many_steps(ctx, timeout_sec=90, steps=None):
            if steps is not None:
                for i in range(13):
                    steps.append(f"额外步骤 {i}")
            return True

        mock_reconnect.side_effect = _inject_many_steps

        # 触发 LOGIN_SCREEN 路径以调用 try_reconnect
        with patch("mhxy_bot.runner.perception.detect_screen_state", return_value=InstanceState.LOGIN_SCREEN):
            ctx = _make_ctx(executor)
            diag = diagnose_instance(ctx)

    # steps 总数不超过 13（12 条实质内容 + 截断提示）
    assert len(diag.steps) <= 13
    if len(diag.steps) == 13:
        assert diag.steps[-1] == "…（已截断）"
