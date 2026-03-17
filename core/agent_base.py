"""
Agent 构建工厂 - 各 bot 通过此函数统一构建 LangChain Agent。
"""
from pathlib import Path
from datetime import datetime
from typing import Callable

from langchain_openai import ChatOpenAI
from langchain.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables.history import RunnableWithMessageHistory
from pydantic import SecretStr

from core.tools.memory_tools import get_session_history as _get_session_history


def build_agent(
    system_prompt: str,
    tools: list,
    dashscope_key: str,
    memory_dir: Path,
    llm_base_url: str = "https://coding.dashscope.aliyuncs.com/v1",
    llm_model: str = "qwen3.5-plus",
    llm_timeout: int = 90,
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
        dashscope_key:  DashScope API Key
        memory_dir:     短期记忆文件目录
        llm_base_url:   LLM 接入点
        llm_model:      模型名称
        llm_timeout:    请求超时秒数

    Returns:
        (agent_with_chat_history, get_user_profile_fn)
        - agent_with_chat_history: 可直接调用 .invoke() / .ainvoke() 的 Agent
        - get_user_profile_fn:     无参函数，读取当前用户长期记忆字符串
    """
    llm = ChatOpenAI(
        model=llm_model,
        api_key=SecretStr(dashscope_key),
        base_url=llm_base_url,
        temperature=0,
        timeout=llm_timeout,
        max_retries=3,
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
