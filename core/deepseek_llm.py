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

        # reasoning_content 在 OpenAI SDK pydantic 对象的 model_extra 里，
        # model_dump() 不包含它，必须直接从原始 choice.message 对象读取
        try:
            choices = (
                response.get("choices") or []
                if isinstance(response, dict)
                else getattr(response, "choices", []) or []
            )
            for i, choice in enumerate(choices):
                if i >= len(result.generations):
                    break
                # 优先从 model_extra 读（SDK 对象路径）
                msg_obj = (
                    choice.get("message") if isinstance(choice, dict)
                    else getattr(choice, "message", None)
                )
                if msg_obj is None:
                    continue
                rc = (
                    msg_obj.get("reasoning_content") if isinstance(msg_obj, dict)
                    else (getattr(msg_obj, "model_extra", None) or {}).get("reasoning_content")
                )
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
