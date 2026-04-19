"""
EHS Bot Agent 构建入口 - EHS（环境/健康/安全）专业知识助理。
"""
import os
from pathlib import Path
from dotenv import load_dotenv

from core.agent_base import build_agent
from core.tools.file_tools import make_file_tools
from core.tools.memory_tools import make_memory_tools, get_user_profile
from core.tools.job_tools import make_job_tools
from ehs_bot.tools.ehs_tools import make_ehs_tools

load_dotenv()

# ==========================================
# 环境变量校验
# ==========================================
MINIMAX_KEY = os.getenv("MINIMAX_API_KEY", "")
EMBEDDING_KEY = os.getenv("DASHSCOPE_APIMODE_KEY", "")

if not MINIMAX_KEY:
    raise ValueError("❌ 致命错误：未在环境变量中找到 MINIMAX_API_KEY！")
if not EMBEDDING_KEY:
    raise ValueError("❌ 致命错误：未在环境变量中找到 DASHSCOPE_APIMODE_KEY！")

# ==========================================
# 目录配置
# ==========================================
BASE_DIR = Path(__file__).parent
SANDBOX_DIR = (BASE_DIR / "../data/ehs/agent_workspace").resolve()
KB_DIR = (BASE_DIR / "../data/ehs/knowledge_base").resolve()
MEMORY_DIR = (BASE_DIR / "../data/ehs/memory").resolve()
FAISS_DIR = (BASE_DIR / "../data/ehs/embeddings").resolve()

for _d in [SANDBOX_DIR, KB_DIR, MEMORY_DIR, FAISS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ==========================================
# System Prompt
# ==========================================
EHS_SYSTEM_PROMPT = """你是一位资深的 EHS（环境、健康与安全）专业顾问，具备以下专业能力：
- 职业健康与安全（OHS/OHSAS 18001/ISO 45001）
- 环境管理体系（ISO 14001、环保法规合规）
- 危险化学品管理（GHS/REACH/RoHS）
- 风险评估与隐患排查（JSA/HAZOP）
- 应急预案编制与演练
- 安全培训与现场审核

🕒 【系统物理时钟】：当前的真实现实时间是 {current_time}。
🧠 【用户的长期记忆库】（以下是关于用户的客观事实，请在分析时主动结合使用）{user_profile}。

==============================
✍️ 工作流：
1. 优先结合知识库中的文档回答专业问题，调用 `analyze_local_document` 检索相关标准或记录。
2. 生成深度分析报告、合规检查清单、风险评估表时，**必须调用 `write_local_file` 保存**，禁止直接在聊天框输出长篇内容。
3. 当用户要求生成定期简报时，调用 `trigger_job` 投递后台任务，不要自行生成。
4. 记忆管理：用户的企业信息、关注法规、持续跟进事项 → 调用 `update_user_memory`。
5. 工作区整理：先调用 `preview_workspace_cleanup` 预览，确认后再调用 `execute_workspace_cleanup`。"""

# ==========================================
# 组装工具列表（暂无领域专属工具，仅使用 core 共享工具）
# ==========================================
_tools = (
    make_file_tools(SANDBOX_DIR, KB_DIR, EMBEDDING_KEY, FAISS_DIR)
    + make_memory_tools(MEMORY_DIR)
    + make_job_tools("ehs_bot.daily_job")
    + make_ehs_tools(SANDBOX_DIR, MEMORY_DIR)
)

# ==========================================
# 构建 Agent
# ==========================================
agent_with_chat_history = build_agent(
    system_prompt=EHS_SYSTEM_PROMPT,
    tools=_tools,
    llm_api_key=MINIMAX_KEY,
    memory_dir=MEMORY_DIR,
    llm_base_url="https://api.minimaxi.com/v1",
    llm_model="MiniMax-M2.7",
    llm_timeout=90,
)


def get_user_profile_fn() -> str:
    """读取当前用户长期记忆字符串（供 TelegramBotBase 注入）。"""
    return get_user_profile(MEMORY_DIR)
