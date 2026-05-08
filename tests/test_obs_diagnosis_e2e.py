from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from core.observability import OmniObserver, OmnibotObsCallbackHandler
from mhxy_bot.runner.models import InstanceDiagnosis, InstanceIssue, InstanceState
from mhxy_bot.tools.game_tools import make_game_tools


def test_check_instance_health_writes_diagnosis_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("MHXY_EXECUTOR_URL", "http://127.0.0.1:1")
    tools = make_game_tools(tmp_path / "sandbox")
    check_instance_health = next(t for t in tools if t.name == "check_instance_health")

    diag = InstanceDiagnosis(
        code=InstanceIssue.ADB_OFFLINE,
        state=InstanceState.OFFLINE,
        needs_human=True,
        message="ADB 未连接",
        steps=["ADB 未连接，尝试诊断模拟器端口"],
    )

    obs = OmniObserver(session_id="s1", agent_id="game-bot", obs_dir=tmp_path / "obs")
    handler = OmnibotObsCallbackHandler(obs, trace_id="t1", provider="test")

    with patch("mhxy_bot.runner.instance_recovery.diagnose_instance", return_value=diag):
        result = asyncio.run(
            check_instance_health.ainvoke(
                {"port": "5557"},
                config={"callbacks": [handler]},
            )
        )

    assert "问题码：adb_offline" in result

    records = [
        json.loads(line)
        for line in (tmp_path / "obs" / "s1.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tool_results = [r for r in records if r.get("type") == "tool_result"]
    assert tool_results
    meta = tool_results[-1].get("meta")
    assert meta == {
        "kind": "instance_diagnosis",
        "port": "127.0.0.1:5557",
        "code": "adb_offline",
        "state": "offline",
        "needs_human": True,
        "steps": ["ADB 未连接，尝试诊断模拟器端口"],
    }
