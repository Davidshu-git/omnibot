"""
DeepSeek thinking 模式兼容层。

问题根源：langchain-openai 0.3.x 的消息转换函数不处理 reasoning_content：
  - 流式路径（_astream）：_convert_chunk_to_generation_chunk 丢弃 delta.reasoning_content
  - 非流式路径（_agenerate）：_create_chat_result 丢弃 reasoning_content
  - 发送时：_convert_message_to_dict 不把 additional_kwargs 写回 dict

DeepSeek 要求：只要上一轮响应含 reasoning_content，下一次请求的 assistant
message 必须原样带上它，否则返回 400。

AgentExecutor 通过 RunnableAgent.aplan() 用 stream_runnable=True 走流式路径，
因此核心修复点在流式 chunk 处理上。

修法：
  1. _convert_chunk_to_generation_chunk — 从流式 delta 提取 reasoning_content
     存入 AIMessageChunk.additional_kwargs；LangChain 的 merge_dicts 会将各 chunk
     的字符串值拼接成最终完整的 reasoning_content。
  2. _create_chat_result  — 从非流式响应提取 reasoning_content（兼容非流式调用）
  3. _get_request_payload — 发送前把 additional_kwargs["reasoning_content"] 注入消息 dict
"""
import logging
from typing import Any, Optional, Union
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import AIMessageChunk
from langchain_core.outputs import ChatResult, ChatGenerationChunk
import openai

logger = logging.getLogger(__name__)


class DeepSeekChatLLM(ChatOpenAI):
    """ChatOpenAI 子类，支持 DeepSeek thinking 模式的双向 reasoning_content 传递。"""

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: Optional[dict],
    ) -> Optional[ChatGenerationChunk]:
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if result is None:
            return result
        try:
            choices = chunk.get("choices", [])
            if choices and isinstance(result.message, AIMessageChunk):
                delta = choices[0].get("delta") or {}
                rc_delta = delta.get("reasoning_content")
                if rc_delta:
                    result.message.additional_kwargs["reasoning_content"] = rc_delta
        except Exception:
            pass
        return result

    def _create_chat_result(
        self,
        response: Union[dict, openai.BaseModel],
        generation_info: Optional[dict] = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        try:
            choices = (
                response.get("choices") or []
                if isinstance(response, dict)
                else getattr(response, "choices", []) or []
            )
            for i, choice in enumerate(choices):
                if i >= len(result.generations):
                    break
                msg_obj = (
                    choice.get("message") if isinstance(choice, dict)
                    else getattr(choice, "message", None)
                )
                if msg_obj is None:
                    continue
                model_extra = getattr(msg_obj, "model_extra", None) or {}
                rc = (
                    msg_obj.get("reasoning_content") if isinstance(msg_obj, dict)
                    else model_extra.get("reasoning_content")
                )
                if rc and isinstance(result.generations[i].message, AIMessage):
                    result.generations[i].message.additional_kwargs["reasoning_content"] = rc
                    logger.debug(f"[DeepSeekLLM] non-stream: injected reasoning_content len={len(rc)}")
        except Exception as e:
            logger.exception(f"[DeepSeekLLM] _create_chat_result error: {e}")
        return result

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict:
        original_messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        msg_dicts = payload.get("messages", [])
        injected = 0
        for msg, msg_dict in zip(original_messages, msg_dicts):
            if isinstance(msg, AIMessage):
                rc = (msg.additional_kwargs or {}).get("reasoning_content")
                if rc:
                    msg_dict["reasoning_content"] = rc
                    injected += 1
        if injected:
            logger.debug(f"[DeepSeekLLM] _get_request_payload: {injected} rc injected into {len(msg_dicts)} messages")
        return payload
