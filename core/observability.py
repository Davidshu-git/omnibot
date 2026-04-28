"""
OmniBot Observability — JSONL session logger + LangChain callback handler.

Writes observability events to per-session-per-day JSONL files.
Format mirrors mhxy_jsonl so the omnibot_jsonl adapter can share the same
mapping logic.

File naming: {obs_dir}/{session_id}.jsonl  (date is embedded in session_id)
Each file starts with a session header record (auto-inserted on first write).
"""
from __future__ import annotations

import ast
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from filelock import FileLock
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------

class OmniObserver:
    """
    Append-only JSONL session logger.

    One file per (session_id, calendar_day) keeps file sizes manageable
    and matches the mhxy daily-rotation convention.
    Thread/process-safe via filelock on each file.
    """

    def __init__(self, session_id: str, agent_id: str, obs_dir: Path) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.obs_dir = obs_dir
        obs_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self) -> Path:
        return self.obs_dir / f"{self.session_id}.jsonl"

    def _write(self, record: dict) -> None:
        path = self._session_path()
        lock_path = path.with_suffix(".lock")
        with FileLock(str(lock_path), timeout=5):
            new_file = not path.exists()
            with open(path, "a", encoding="utf-8") as f:
                if new_file:
                    # Insert session header at the start of each daily file
                    session_rec: dict[str, Any] = {
                        "type": "session",
                        "id": self.session_id,
                        "timestamp": _now_iso(),
                        "channel": "tg",
                        "agent_id": self.agent_id,
                    }
                    f.write(json.dumps(session_rec, ensure_ascii=False) + "\n")
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _now(self) -> str:
        """Compatibility helper for mhxy-style log callbacks."""
        return _now_iso()

    def write_raw_event(self, record: dict) -> None:
        """
        Append a pre-shaped JSONL event.

        This keeps migrated mhxy helpers that emit model_call records with
        prompt/raw_output compatible without coupling the bot to a remote API.
        """
        rec = dict(record)
        rec.setdefault("timestamp", _now_iso())
        self._write(rec)

    def _write_log(self, record: dict) -> None:
        """Compatibility shim for mhxy.tools.core.session_logger callbacks."""
        self.write_raw_event(record)

    # ------------------------------------------------------------------
    # Public log methods — one per event type
    # ------------------------------------------------------------------

    def log_message(
        self, role: str, content: str, trace_id: str | None = None
    ) -> None:
        rec: dict[str, Any] = {
            "type": "message",
            "timestamp": _now_iso(),
            "role": role,
            "content": content,
        }
        if trace_id:
            rec["trace_id"] = trace_id
        self._write(rec)

    def log_thought(self, content: str, trace_id: str | None = None) -> None:
        if not content.strip():
            return
        rec: dict[str, Any] = {
            "type": "thought",
            "timestamp": _now_iso(),
            "content": content,
        }
        if trace_id:
            rec["trace_id"] = trace_id
        self._write(rec)

    def log_model_call(
        self,
        model: str,
        provider: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_write_tokens: int | None,
        total_tokens: int | None,
        duration_ms: float | None,
        stop_reason: str | None,
        error_message: str | None,
        trace_id: str | None,
        run_id: str | None,
    ) -> None:
        rec: dict[str, Any] = {
            "type": "model_call",
            "timestamp": _now_iso(),
            "model": model,
            "provider": provider,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "total_tokens": total_tokens,
            "duration_ms": duration_ms,
            "stop_reason": stop_reason,
            "error_message": error_message,
        }
        if trace_id:
            rec["trace_id"] = trace_id
        if run_id:
            rec["run_id"] = run_id
        self._write(rec)

    def log_tool_call(
        self,
        tool_name: str,
        arguments: Any,
        trace_id: str | None,
        run_id: str | None,
    ) -> None:
        rec: dict[str, Any] = {
            "type": "tool_call",
            "timestamp": _now_iso(),
            "tool_name": tool_name,
            "arguments": arguments,
        }
        if trace_id:
            rec["trace_id"] = trace_id
        if run_id:
            rec["run_id"] = run_id
        self._write(rec)

    def log_tool_result(
        self,
        tool_name: str,
        output: str,
        success: bool,
        duration_ms: float | None,
        error_message: str | None,
        trace_id: str | None,
        run_id: str | None,
    ) -> None:
        rec: dict[str, Any] = {
            "type": "tool_result",
            "timestamp": _now_iso(),
            "tool_name": tool_name,
            "output": output,
            "success": success,
            "duration_ms": duration_ms,
            "error_message": error_message,
        }
        if trace_id:
            rec["trace_id"] = trace_id
        if run_id:
            rec["run_id"] = run_id
        self._write(rec)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def extract_think_blocks(text: str) -> list[str]:
    """Return all non-empty <think>…</think> blocks from LLM output."""
    return [m.strip() for m in _THINK_RE.findall(text) if m.strip()]


def strip_think_blocks(text: str) -> str:
    """Remove all <think>…</think> blocks and collapse extra blank lines."""
    return _THINK_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# LangChain callback handler
# ---------------------------------------------------------------------------

class OmnibotObsCallbackHandler(AsyncCallbackHandler):
    """
    Writes model_call, tool_call, tool_result events to OmniObserver via
    LangChain's async callback interface.

    Attach alongside AsyncTelegramCallbackHandler in execute_agent_task:
        callbacks=[tg_callback, obs_callback]
    """

    def __init__(self, observer: OmniObserver, trace_id: str, provider: str = "dashscope") -> None:
        super().__init__()
        self._obs = observer
        self._trace_id = trace_id
        self._provider = provider
        self._run_counter = 0
        self._current_run_id: str | None = None
        self._llm_start_time: float | None = None
        self._llm_model: str = "unknown"
        # Keyed by LangChain run_id (UUID str) so concurrent tool calls don't clash
        self._tool_start_times: dict[str, float] = {}
        self._tool_names: dict[str, str] = {}

    # ------------------------------------------------------------------
    # LLM events
    # ------------------------------------------------------------------

    def _on_llm_start_common(self, serialized: dict) -> None:
        self._run_counter += 1
        self._current_run_id = f"{self._trace_id}:r{self._run_counter}"
        self._llm_start_time = time.perf_counter()
        kw = serialized.get("kwargs") or {}
        self._llm_model = kw.get("model_name") or kw.get("model") or "unknown"

    async def on_llm_start(
        self,
        serialized: dict,
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        self._on_llm_start_common(serialized)

    async def on_chat_model_start(
        self,
        serialized: dict,
        messages: list,
        **kwargs: Any,
    ) -> None:
        self._on_llm_start_common(serialized)

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        duration_ms: float | None = None
        if self._llm_start_time is not None:
            duration_ms = round((time.perf_counter() - self._llm_start_time) * 1000, 1)
            self._llm_start_time = None

        lo = response.llm_output or {}
        stop_reason: str | None = lo.get("finish_reason") or lo.get("stop_reason")
        input_tok: int | None = None
        output_tok: int | None = None
        total_tok: int | None = None
        cache_read: int | None = None
        cache_write: int | None = None

        # Path 1: AgentExecutor streaming — tokens land in message.usage_metadata
        if response.generations:
            g = response.generations[0][0] if response.generations[0] else None
            if g is not None:
                um = getattr(getattr(g, "message", None), "usage_metadata", None)
                if um:
                    input_tok = um.get("input_tokens")
                    output_tok = um.get("output_tokens")
                    total_tok = um.get("total_tokens")
                    in_details = um.get("input_token_details") or {}
                    cache_read = (
                        in_details.get("cache_read")
                        or in_details.get("cached_tokens")
                    ) or None
                    cache_write = in_details.get("cache_creation") or None
                # response_metadata 里读原始 token_usage，补 LangChain 未映射的字段
                meta = getattr(getattr(g, "message", None), "response_metadata", None) or {}
                raw_usage = meta.get("token_usage") or {}
                if not stop_reason:
                    stop_reason = meta.get("finish_reason") or meta.get("stop_reason")
                if cache_read is None:
                    prompt_details = raw_usage.get("prompt_tokens_details") or {}
                    cache_read = prompt_details.get("cached_tokens") or None

        # Path 2: non-streaming / direct ainvoke — tokens in llm_output["token_usage"]
        if input_tok is None:
            usage: dict = lo.get("token_usage") or lo.get("usage") or {}
            input_tok = usage.get("prompt_tokens") or usage.get("input_tokens")
            output_tok = usage.get("completion_tokens") or usage.get("output_tokens")
            total_tok = usage.get("total_tokens")
            cache_read = usage.get("cache_read_tokens") or usage.get("prompt_cache_hit_tokens")
            cache_write = usage.get("cache_write_tokens") or usage.get("prompt_cache_miss_tokens")
            if not stop_reason:
                stop_reason = usage.get("finish_reason") or usage.get("stop_reason")

        model = lo.get("model_name") or self._llm_model

        # DeepSeek native reasoning_content (stored in additional_kwargs by deepseek_llm.py)
        if response.generations:
            g = response.generations[0][0] if response.generations[0] else None
            if g is not None:
                rc = (getattr(getattr(g, "message", None), "additional_kwargs", None) or {}).get("reasoning_content")
                if rc and rc.strip():
                    self._obs.log_thought(rc.strip(), trace_id=self._trace_id)

        self._obs.log_model_call(
            model=model,
            provider=self._provider,
            input_tokens=input_tok,
            output_tokens=output_tok,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            total_tokens=total_tok,
            duration_ms=duration_ms,
            stop_reason=stop_reason,
            error_message=None,
            trace_id=self._trace_id,
            run_id=self._current_run_id,
        )

    # ------------------------------------------------------------------
    # Tool events
    # ------------------------------------------------------------------

    async def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        **kwargs: Any,
    ) -> None:
        lc_run_id = str(kwargs.get("run_id", ""))
        tool_name = serialized.get("name", "unknown")
        self._tool_start_times[lc_run_id] = time.perf_counter()
        self._tool_names[lc_run_id] = tool_name

        # LangChain passes tool input as Python repr string; try multiple parse strategies
        arguments: Any = None
        try:
            arguments = ast.literal_eval(input_str)
        except (ValueError, SyntaxError):
            try:
                arguments = json.loads(input_str)
            except (json.JSONDecodeError, TypeError):
                arguments = {"_raw": input_str}

        self._obs.log_tool_call(
            tool_name=tool_name,
            arguments=arguments,
            trace_id=self._trace_id,
            run_id=self._current_run_id,
        )

    async def on_tool_end(self, output: str, **kwargs: Any) -> None:
        lc_run_id = str(kwargs.get("run_id", ""))
        tool_name = self._tool_names.pop(lc_run_id, "unknown")
        start_t = self._tool_start_times.pop(lc_run_id, time.perf_counter())
        duration_ms = round((time.perf_counter() - start_t) * 1000, 1)

        self._obs.log_tool_result(
            tool_name=tool_name,
            output=str(output)[:4096],
            success=True,
            duration_ms=duration_ms,
            error_message=None,
            trace_id=self._trace_id,
            run_id=self._current_run_id,
        )

    async def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        lc_run_id = str(kwargs.get("run_id", ""))
        tool_name = self._tool_names.pop(lc_run_id, "unknown")
        start_t = self._tool_start_times.pop(lc_run_id, time.perf_counter())
        duration_ms = round((time.perf_counter() - start_t) * 1000, 1)

        self._obs.log_tool_result(
            tool_name=tool_name,
            output="",
            success=False,
            duration_ms=duration_ms,
            error_message=str(error),
            trace_id=self._trace_id,
            run_id=self._current_run_id,
        )
