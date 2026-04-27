"""
Agent 构建工厂 - 各 bot 通过此函数统一构建 LangChain Agent。
"""
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Callable

from langchain_openai import ChatOpenAI
from langchain.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables.history import RunnableWithMessageHistory
from pydantic import SecretStr

from core.tools.memory_tools import get_session_history as _get_session_history

logger = logging.getLogger(__name__)


def build_agent(
    system_prompt: str,
    tools: list,
    llm_api_key: str,
    memory_dir: Path,
    llm_base_url: str = "https://coding.dashscope.aliyuncs.com/v1",
    llm_model: str = "qwen3.5-plus",
    llm_timeout: int = 90,
    llm_max_tokens: int = 8192,
    llm_model_kwargs: dict | None = None,
    llm_extra_body: dict | None = None,
):
    """
    构建带记忆的 LangChain Agent。

    system_prompt 必须是完整的系统提示文本，可包含以下占位符（会在每次调用时动态注入）：
      - {current_time}   当前时间
      - {user_profile}   用户长期记忆
      - {chat_history}   短期对话历史（自动注入，无需手动处理）
      - {input}          用户输入
      - {agent_scratchpad} Agent 推理过程

    Args:
        system_prompt:  完整系统提示，各 bot 自定义
        tools:          工具列表（core 工具 + 领域工具合并后传入）
        llm_api_key:    LLM API Key
        memory_dir:     短期记忆文件目录
        llm_base_url:   LLM 接入点
        llm_model:      模型名称
        llm_timeout:    请求超时秒数
        llm_max_tokens: 最大输出 token 数

    Returns:
        (agent_with_chat_history, get_user_profile_fn)
        - agent_with_chat_history: 可直接调用 .invoke() / .ainvoke() 的 Agent
        - get_user_profile_fn:     无参函数，读取当前用户长期记忆字符串
    """
    llm = ChatOpenAI(
        model=llm_model,
        api_key=SecretStr(llm_api_key),
        base_url=llm_base_url,
        temperature=0.2,
        timeout=llm_timeout,
        max_retries=3,
        max_tokens=llm_max_tokens,
        stream_usage=True,
        model_kwargs=llm_model_kwargs or {},
        **({"extra_body": llm_extra_body} if llm_extra_body else {}),
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("placeholder", "{chat_history}"),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])

    agent = create_tool_calling_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=False)

    def _get_history(session_id: str):
        return _get_session_history(memory_dir, session_id)

    agent_with_chat_history = RunnableWithMessageHistory(
        agent_executor,
        _get_history,
        input_messages_key="input",
        history_messages_key="chat_history",
    )

    return agent_with_chat_history


# ---------------------------------------------------------------------------
# _DynamicAgent + build_dynamic_agent
# ---------------------------------------------------------------------------

class _DynamicAgent:
    """
    动态模型路由 Agent 包装器。
    根据 registry.current_key() 懒初始化并缓存 RunnableWithMessageHistory 实例。
    ainvoke / invoke / astream 与 RunnableWithMessageHistory 接口相同。
    """

    def __init__(
        self,
        registry,           # ModelRegistry，运行时 import 避免循环依赖
        system_prompt: str,
        tools: list,
        memory_dir: Path,
        **build_kwargs,     # 透传给 build_agent 的额外参数（如 llm_model_kwargs）
    ) -> None:
        self._registry = registry
        self._system_prompt = system_prompt
        self._tools = tools
        self._memory_dir = memory_dir
        self._build_kwargs = build_kwargs
        self._cache: dict[str, RunnableWithMessageHistory] = {}

    def _build(self, key: str) -> RunnableWithMessageHistory:
        cfg = self._registry._configs[key]
        agent = build_agent(
            system_prompt=self._system_prompt,
            tools=self._tools,
            llm_api_key=cfg.api_key,
            memory_dir=self._memory_dir,
            llm_base_url=cfg.base_url,
            llm_model=cfg.model,
            llm_timeout=cfg.timeout,
            llm_max_tokens=cfg.max_tokens,
            llm_extra_body=cfg.extra_body,
            **self._build_kwargs,
        )
        logger.info(f"[_DynamicAgent] 已构建 agent 实例：key={key}")
        return agent

    def _agent(self) -> RunnableWithMessageHistory:
        key = self._registry.current_key()
        if key not in self._cache:
            self._cache[key] = self._build(key)
        return self._cache[key]

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        return await self._agent().ainvoke(*args, **kwargs)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return self._agent().invoke(*args, **kwargs)

    async def astream(self, *args: Any, **kwargs: Any):
        async for chunk in self._agent().astream(*args, **kwargs):
            yield chunk


def build_dynamic_agent(
    registry,
    system_prompt: str,
    tools: list,
    memory_dir: Path,
    **kwargs,
) -> _DynamicAgent:
    """
    构建一个 _DynamicAgent，内部按 model key 懒初始化并缓存 agent 实例。
    registry: ModelRegistry 实例
    其余参数与 build_agent 相同（除 llm_api_key / llm_base_url / llm_model 由 registry 提供）。
    """
    return _DynamicAgent(
        registry=registry,
        system_prompt=system_prompt,
        tools=tools,
        memory_dir=memory_dir,
        **kwargs,
    )
