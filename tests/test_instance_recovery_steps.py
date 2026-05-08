# -*- coding: utf-8 -*-
"""测试 diagnose_instance 的 steps 累加逻辑及 try_reconnect 事件发射。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from mhxy_bot.runner.context import RunnerContext
from mhxy_bot.runner.instance_recovery import diagnose_instance, try_reconnect
from mhxy_bot.runner.models import InstanceIssue, InstanceState
from mhxy_bot.tools.executor_client import ExecutorClient


def _make_ctx(executor: ExecutorClient | None = None) -> RunnerContext:
    return RunnerContext(
        executor=executor or MagicMock(spec=ExecutorClient),
        port="5557",
    )


def _fake_ocr_main_ui(ctx):
    return (InstanceState.MAIN_UI, ["任务", "队伍"],
            [{"text": "任务", "center_x": 100, "center_y": 200, "confidence": 0.9},
             {"text": "队伍", "center_x": 100, "center_y": 300, "confidence": 0.9}])


# ---------------------------------------------------------------------------
# diagnose_instance 基础测试
# ---------------------------------------------------------------------------

def test_adb_offline_adds_step():
    """ADB 离线时 steps 应包含 "ADB 未连接"。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": False}

    ctx = _make_ctx(executor)
    diag = diagnose_instance(ctx)

    assert diag.steps, "steps 不应为空"
    assert any("ADB 未连接" in s for s in diag.steps)
    assert diag.code == InstanceIssue.ADB_OFFLINE


def test_normal_path_steps_ok():
    """正常路径 steps 应包含屏幕状态，needs_human=False。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}
    executor.sense.return_value = [{"text": "任务", "center_x": 100, "center_y": 200, "confidence": 0.9}]

    with patch("mhxy_bot.runner.perception.detect_with_texts", side_effect=_fake_ocr_main_ui):
        ctx = _make_ctx(executor)
        diag = diagnose_instance(ctx)

    assert any("屏幕状态" in s and "main_ui" in s for s in diag.steps)
    assert diag.code == InstanceIssue.UNKNOWN_OK
    assert diag.needs_human is False


def test_disconnected_diagnosis_does_not_try_reconnect_by_default():
    """诊断掉线状态默认只上报，不应直接触发 try_reconnect。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}

    with patch("mhxy_bot.runner.perception.detect_with_texts",
               return_value=(InstanceState.GAME_DISCONNECTED, [], [])), \
         patch("mhxy_bot.runner.instance_recovery.try_reconnect") as mock_reconnect:
        ctx = _make_ctx(executor)
        diag = diagnose_instance(ctx)

    mock_reconnect.assert_not_called()
    assert diag.code == InstanceIssue.GAME_DISCONNECTED
    assert diag.needs_human is True


def test_login_screen_diagnosis_does_not_try_reconnect_by_default():
    """诊断登录界面默认只上报，不应直接触发 try_reconnect。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}

    with patch("mhxy_bot.runner.perception.detect_with_texts",
               return_value=(InstanceState.LOGIN_SCREEN, [], [])), \
         patch("mhxy_bot.runner.instance_recovery.try_reconnect") as mock_reconnect:
        ctx = _make_ctx(executor)
        diag = diagnose_instance(ctx)

    mock_reconnect.assert_not_called()
    assert diag.code == InstanceIssue.LOGIN_SCREEN
    assert diag.needs_human is True


def test_attempt_reconnect_success_writes_step_when_explicitly_enabled():
    """显式开启 attempt_reconnect 时，诊断应执行重连并返回成功结果。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.app_health.return_value = {"adb": True, "screenshot": True, "ocr": True}
    executor.tap.return_value = True

    call_count = 0

    def _detect_side_effect(ctx):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (InstanceState.GAME_DISCONNECTED, [], [])
        return (InstanceState.MAIN_UI, ["任务", "队伍"], [])

    with patch("mhxy_bot.runner.perception.detect_with_texts", side_effect=_detect_side_effect):
        ctx = _make_ctx(executor)
        diag = diagnose_instance(ctx, attempt_reconnect=True)

    # 应成功恢复：code=UNKNOWN_OK, needs_human=False
    assert diag.code == InstanceIssue.UNKNOWN_OK
    assert diag.needs_human is False
    assert diag.state == InstanceState.MAIN_UI
    # steps 应记录重连过程
    assert any("掉线" in s for s in diag.steps)
    assert any("自动恢复成功" in s for s in diag.steps)


# ---------------------------------------------------------------------------
# try_reconnect 事件发射测试
# ---------------------------------------------------------------------------

def _make_reconnect_mock():
    """创建 reconnect_step mock 并返回 (mock, 收集函数) 元组。"""
    captured = []

    def _capture(*args, **kwargs):
        # args: (ctx, state_value, action)
        captured.append({
            "state": args[1] if len(args) > 1 else kwargs.get("state"),
            "action": args[2] if len(args) > 2 else kwargs.get("action"),
            "success": kwargs.get("success", True),
            "detail": kwargs.get("detail", ""),
        })

    mock = MagicMock(side_effect=_capture)
    return mock, captured


def test_try_reconnect_emits_step_events():
    """try_reconnect 循环内每轮都应 emit reconnect_step 事件。"""
    executor = MagicMock(spec=ExecutorClient)
    executor.tap.return_value = True

    mock_step, captured = _make_reconnect_mock()

    with patch("mhxy_bot.runner.events.reconnect_step", mock_step), \
         patch("mhxy_bot.runner.perception.detect_with_texts") as mock_detect, \
         patch("time.monotonic", side_effect=[0, 3, 6, 9]):

        def _state_sequence(ctx):
            states = [
                (InstanceState.GAME_DISCONNECTED, ["重新登录"],
                 [{"text": "重新登录", "center_x": 400, "center_y": 300, "confidence": 0.9}]),
                (InstanceState.LOGIN_SCREEN, ["登录游戏"],
                 [{"text": "登录游戏", "center_x": 400, "center_y": 300, "confidence": 0.9}]),
                (InstanceState.MAIN_UI, ["任务", "队伍"],
                 [{"text": "任务", "center_x": 100, "center_y": 200, "confidence": 0.9}]),
            ]
            idx = _state_sequence.idx
            _state_sequence.idx += 1
            return states[idx]

        _state_sequence.idx = 0
        mock_detect.side_effect = _state_sequence

        ctx = _make_ctx(executor)
        result = try_reconnect(ctx, timeout_sec=10)

    assert result is True
    # 每轮有 detect + action 两个事件，共 3 轮 → 6 条
    assert len(captured) == 6

    assert captured[0]["state"] == "game_disconnected"
    assert captured[0]["action"] == "detect"
    assert captured[1]["action"] == "tap"
    assert captured[2]["state"] == "login_screen"
    assert captured[2]["action"] == "detect"
    assert captured[3]["action"] == "tap"
    assert captured[4]["state"] == "main_ui"
    assert captured[4]["action"] == "detect"
    assert captured[5]["action"] == "success"


def test_try_reconnect_emits_timeout_event():
    """超时时应 emit 一条 timeout 类型的 reconnect_step。"""
    executor = MagicMock(spec=ExecutorClient)

    mock_step, captured = _make_reconnect_mock()

    with patch("mhxy_bot.runner.events.reconnect_step", mock_step), \
         patch("mhxy_bot.runner.perception.detect_with_texts",
               return_value=(InstanceState.UNKNOWN, [], [])), \
         patch("time.monotonic", side_effect=[0, 3, 6, 12]):  # 3 rounds, timeout at 10

        ctx = _make_ctx(executor)
        result = try_reconnect(ctx, timeout_sec=10)

    assert result is False

    timeout_events = [e for e in captured if e["action"] == "timeout"]
    assert len(timeout_events) == 1
    assert timeout_events[0]["success"] is False


def test_try_reconnect_emits_back_event_for_popup():
    """弹窗场景应 emit back 动作的 reconnect_step。"""
    executor = MagicMock(spec=ExecutorClient)

    mock_step, captured = _make_reconnect_mock()

    with patch("mhxy_bot.runner.events.reconnect_step", mock_step), \
         patch("mhxy_bot.runner.perception.detect_with_texts") as mock_detect, \
         patch("time.monotonic", side_effect=[0, 3, 6]):

        def _state_seq(ctx):
            _state_seq.idx += 1
            if _state_seq.idx == 1:
                return (InstanceState.ACTIVITY_POPUP, ["点击任意空白处关闭界面"], [])
            return (InstanceState.MAIN_UI, ["任务", "队伍"], [])

        _state_seq.idx = 0
        mock_detect.side_effect = _state_seq
        executor.back.return_value = True

        ctx = _make_ctx(executor)
        result = try_reconnect(ctx, timeout_sec=10)

    assert result is True

    back_events = [e for e in captured if e["action"] == "back"]
    assert len(back_events) == 1
    assert back_events[0]["state"] == "activity_popup"
