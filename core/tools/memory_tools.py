"""
记忆系统工具工厂 - LTM/STM 双轨记忆架构。
"""
import json
from pathlib import Path
from filelock import FileLock
from langchain_core.tools import tool
from langchain_community.chat_message_histories import FileChatMessageHistory
from langchain_core.messages import BaseMessage, AIMessage

import logging

logger = logging.getLogger(__name__)


def get_user_profile(memory_dir: Path) -> str:
    """读取 KV 结构的长期记忆（供 agent 注入 system prompt）"""
    profile_path = memory_dir / "user_profile.json"
    if not profile_path.exists():
        return "暂无长期记忆"
    try:
        with open(profile_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not data:
            return "暂无长期记忆"
        return "\n".join([f"- 【{k}】: {v}" for k, v in data.items()])
    except Exception as e:
        logger.warning(f"读取长期记忆失败：{type(e).__name__}")
        return "暂无长期记忆"


class _CleanFileChatMessageHistory(FileChatMessageHistory):
    """FileChatMessageHistory 子类：写入时剥离 reasoning_content。

    部分模型（DeepSeek thinking、DashScope）在 AIMessage.additional_kwargs 中
    返回 reasoning_content，但下一轮回放时不允许带着它，否则 API 报 400。
    """
    def add_message(self, message: BaseMessage) -> None:
        if isinstance(message, AIMessage):
            rc = message.additional_kwargs.get("reasoning_content")
            if rc:
                clean_kwargs = {k: v for k, v in message.additional_kwargs.items()
                                if k != "reasoning_content"}
                message = AIMessage(
                    content=message.content,
                    additional_kwargs=clean_kwargs,
                    tool_calls=getattr(message, "tool_calls", []) or [],
                    usage_metadata=getattr(message, "usage_metadata", None),
                )
        super().add_message(message)


def get_session_history(memory_dir: Path, session_id: str):
    """带滑动窗口的短期记忆引擎（10 条消息）"""
    memory_file = str(memory_dir / f"{session_id}.json")
    history = _CleanFileChatMessageHistory(memory_file)
    if len(history.messages) > 10:
        kept = history.messages[-10:]
        history.clear()
        for msg in kept:
            history.add_message(msg)
    return history


def make_memory_tools(memory_dir: Path) -> list:
    """
    创建绑定了具体记忆目录的 LangChain 工具列表。

    Args:
        memory_dir: 记忆文件目录（存放 user_profile.json、transaction_logs.jsonl）
    """
    memory_dir.mkdir(parents=True, exist_ok=True)
    profile_path = memory_dir / "user_profile.json"
    lock_path = memory_dir / "user_profile.json.lock"
    log_path = memory_dir / "transaction_logs.jsonl"

    @tool
    def update_user_memory(key: str, value: str) -> str:
        """
        🚨【记忆更新指令】：
        用于记录或更新用户的状态、偏好、持仓快照。相同 key 会直接覆盖。
        - 参数 key: 记忆的分类标签，必须是简短明确的名词（例如："风险偏好"、"报告格式要求"）。
        - 参数 value: 具体的客观事实数据。
        """
        try:
            if not profile_path.exists():
                with open(profile_path, 'w', encoding='utf-8') as f:
                    json.dump({}, f)
            with FileLock(lock_path, timeout=5):
                with open(profile_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                data[key] = value
                with open(profile_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            return f"✅ 记忆已安全写入（加锁保护）：[{key}] -> '{value}'"
        except json.JSONDecodeError:
            return "❌ 记忆文件损坏：JSONDecodeError"
        except TimeoutError:
            return "❌ 文件锁超时：其他进程正在写入记忆"
        except Exception as e:
            return f"记忆写入失败：{type(e).__name__} - {str(e)}"

    @tool
    def append_transaction_log(action: str, target: str, details: str) -> str:
        """
        🚨【交易日志指令】：
        仅当用户明确发生了一笔【交易动作】（如：买入、卖出、转账）时调用。
        它会像流水账一样把这笔操作追加到数据库中，绝对不会覆盖过去的历史。
        """
        import time
        try:
            entry = json.dumps({
                "timestamp": time.time(),
                "action": action,
                "target": target,
                "details": details
            }, ensure_ascii=False)
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(entry + "\n")
            return "✅ 流水已追加记录。"
        except Exception as e:
            return f"记录流水失败：{type(e).__name__} - {str(e)}"

    return [update_user_memory, append_transaction_log]
