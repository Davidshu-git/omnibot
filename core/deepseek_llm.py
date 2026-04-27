"""
DeepSeek thinking 模式兼容层。

问题根源：langchain-openai 0.3.x 的消息转换函数不处理 reasoning_content：
  - _convert_dict_to_message：响应转 AIMessage 时丢弃 reasoning_content
  - _convert_message_to_dict：发送时不把 additional_kwargs 写回 dict

DeepSeek 要求：只要上一轮响应含 reasoning_content，下一次请求的 assistant
message 必须原样带上它，否则返回 400。

修法：
  1. _create_chat_result  — 从原始响应提取 reasoning_content，存入 AIMessage.additional_kwargs
  2. _get_request_payload — 发送前把 additional_kwargs["reasoning_content"] 注入回消息 dict
"""
from typing import Any, Optional, Union
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult
import openai


class DeepSeekChatLLM(ChatOpenAI):
    """ChatOpenAI 子类，支持 DeepSeek thinking 模式的双向 reasoning_content 传递。"""

    def _create_chat_result(
        self,
        response: Union[dict, openai.BaseModel],
        generation_info: Optional[dict] = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)

        # 从原始响应提取 reasoning_content，注入每个 generation 的 AIMessage
        try:
            resp_dict = (
                response if isinstance(response, dict) else response.model_dump()
            )
            choices = resp_dict.get("choices") or []
            for i, choice in enumerate(choices):
                if i >= len(result.generations):
                    break
                msg_dict = choice.get("message") or {}
                rc = msg_dict.get("reasoning_content")
                if rc and isinstance(result.generations[i].message, AIMessage):
                    result.generations[i].message.additional_kwargs["reasoning_content"] = rc
        except Exception:
            pass

        return result

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict:
        # 先拿到原始 message 对象（含 additional_kwargs）
        original_messages = self._convert_input(input_).to_messages()

        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # 把 reasoning_content 注入已转换的 message dict
        msg_dicts = payload.get("messages", [])
        for msg, msg_dict in zip(original_messages, msg_dicts):
            if isinstance(msg, AIMessage):
                rc = (msg.additional_kwargs or {}).get("reasoning_content")
                if rc:
                    msg_dict["reasoning_content"] = rc

        return payload
