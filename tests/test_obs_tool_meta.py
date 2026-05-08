from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from langchain_core.tools import tool

from core.observability import (
    OmniObserver,
    OmnibotObsCallbackHandler,
    attach_tool_meta,
)


def _run(coro):
    return asyncio.run(coro)


def make_handler():
    obs = MagicMock(spec=OmniObserver)
    h = OmnibotObsCallbackHandler(obs, trace_id="t1", provider="test")
    return obs, h


@tool
def _t_attach(payload: str) -> str:
    """Probe attaching tool metadata."""
    attach_tool_meta({"kind": "x", "payload": payload})
    return "ok"


@tool
def _t_skip(payload: str) -> str:
    """Probe without attaching tool metadata."""
    return "ok"


def test_attach_to_tool_end():
    obs, h = make_handler()
    _run(_t_attach.ainvoke({"payload": "v"}, config={"callbacks": [h]}))
    obs.log_tool_result.assert_called_once()
    kwargs = obs.log_tool_result.call_args.kwargs
    assert kwargs["meta"] == {"kind": "x", "payload": "v"}


def test_no_attach_yields_none():
    obs, h = make_handler()
    _run(_t_skip.ainvoke({"payload": "v"}, config={"callbacks": [h]}))
    assert obs.log_tool_result.call_args.kwargs["meta"] is None


def test_sequential_no_leak():
    obs, h = make_handler()
    _run(_t_attach.ainvoke({"payload": "1"}, config={"callbacks": [h]}))
    _run(_t_skip.ainvoke({"payload": "2"}, config={"callbacks": [h]}))
    metas = [c.kwargs["meta"] for c in obs.log_tool_result.call_args_list]
    assert metas[0] == {"kind": "x", "payload": "1"}
    assert metas[1] is None


def test_size_limit_drops():
    obs, h = make_handler()
    big = {"x": "y" * 5000}

    @tool
    def _t_big(payload: str) -> str:
        """Probe oversized tool metadata."""
        attach_tool_meta(big)
        return "ok"

    _run(_t_big.ainvoke({"payload": "."}, config={"callbacks": [h]}))
    assert obs.log_tool_result.call_args.kwargs["meta"] is None


def test_error_path_carries_meta():
    obs, h = make_handler()

    @tool
    def _t_err(payload: str) -> str:
        """Probe error path with tool metadata."""
        attach_tool_meta({"kind": "x"})
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _run(_t_err.ainvoke({"payload": "."}, config={"callbacks": [h]}))
    assert obs.log_tool_result.call_args.kwargs["meta"] == {"kind": "x"}
