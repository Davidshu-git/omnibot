"""
记忆系统工具工厂 - LTM/STM 双轨记忆架构。
"""
import json
import tiktoken
from pathlib import Path
from filelock import FileLock
from langchain_core.tools import tool
from langchain_community.chat_message_histories import FileChatMessageHistory

import logging

logger = logging.getLogger(__name__)

# 预留给 system prompt + 工具定义 + 本轮用户消息 + 模型回复的 token 空间
# 历史记忆可用预算 = 上下文窗口 - 此预留量
_RESERVED_TOKENS = 16000
# 默认历史 token 预算
_DEFAULT_HISTORY_BUDGET = 32000

_encoder = tiktoken.get_encoding("cl100k_base")  # 对 Qwen 模型偏保守估计，安全


def _count_message_tokens(messages: list) -> int:
    """估算消息列表的 token 数（cl100k_base，对中文偏高估，保守安全）。"""
    total = 0
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        total += len(_encoder.encode(content)) + 4  # 每条消息约 4 token 额外开销
    return total


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


def get_session_history(
    memory_dir: Path,
    session_id: str,
    history_token_budget: int = _DEFAULT_HISTORY_BUDGET,
):
    """基于 token 预算的短期记忆引擎。

    从最旧的消息对（human+ai）开始丢弃，直到历史 token 数在预算内。
    始终保留最近至少 2 条消息（1 轮），避免上下文完全丢失。
    """
    memory_file = str(memory_dir / f"{session_id}.json")
    history = FileChatMessageHistory(memory_file)
    messages = list(history.messages)

    if not messages:
        return history

    original_count = len(messages)

    # 从头部每次裁掉 2 条（一对 human+ai），直到 token 数满足预算
    while len(messages) > 2 and _count_message_tokens(messages) > history_token_budget:
        messages = messages[2:]

    if len(messages) != original_count:
        logger.debug(
            f"[memory] {session_id}: 裁剪历史 {original_count} → {len(messages)} 条消息"
            f"（budget={history_token_budget} tokens）"
        )
        history.clear()
        for msg in messages:
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
