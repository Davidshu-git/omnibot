"""
Stock Bot Agent 构建入口 - 组合 core 共享工具与股票领域专属工具，构建完整 Agent。
"""
import os
from pathlib import Path
from dotenv import load_dotenv

from core.agent_base import build_agent
from core.tools.file_tools import make_file_tools
from core.tools.memory_tools import make_memory_tools, get_user_profile
from core.tools.job_tools import make_job_tools
from stock_bot.tools.stock_tools import make_stock_tools

load_dotenv()

# ==========================================
# 环境变量校验
# ==========================================
MINIMAX_KEY = os.getenv("ALI_CODING_PLAN_KEY", "")
EMBEDDING_KEY = os.getenv("DASHSCOPE_APIMODE_KEY", "")

if not MINIMAX_KEY:
    raise ValueError("❌ 致命错误：未在环境变量中找到 ALI_CODING_PLAN_KEY！")
if not EMBEDDING_KEY:
    raise ValueError("❌ 致命错误：未在环境变量中找到 DASHSCOPE_APIMODE_KEY！")

# ==========================================
# 目录配置
# ==========================================
BASE_DIR = Path(__file__).parent
SANDBOX_DIR = (BASE_DIR / "../data/stock/agent_workspace").resolve()
KB_DIR = (BASE_DIR / "../data/stock/knowledge_base").resolve()
MEMORY_DIR = (BASE_DIR / "../data/stock/memory").resolve()
FAISS_DIR = (BASE_DIR / "../data/stock/embeddings").resolve()

for _d in [SANDBOX_DIR, KB_DIR, MEMORY_DIR, FAISS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ==========================================
# System Prompt
# ==========================================
STOCK_SYSTEM_PROMPT = """你是一个极客风格的全栈量化分析师与系统助手。
 🕒 【系统物理时钟】：当前的真实现实时间是 {current_time}。你需要以此为绝对基准来理解用户的相对时间描述（如"今天"、"上周"、"昨天"），并判断当前所处的交易周期。
 🧠 【用户的长期记忆库】(以下是关于用户的客观事实，请在分析时主动结合使用){user_profile}。
 ==============================
🔍 核心能力：
- 遇到 ETF 基金查价（如 513050、159915 等 6 位数字代码），优先调用 `get_etf_price`；
- 遇到股票查价，调用 `get_universal_stock_price`；
- 遇到画图需求，调用 `draw_universal_stock_chart`；
- 遇到"设置预警/盯盘"，调用 `create_price_alert`；
- 遇到"查询预警/有哪些盯盘"，调用 `list_price_alerts`；
- 遇到"取消/删除预警"，调用 `delete_price_alert`。
工具会在底层自动识别美股/A 股/港股，你无需操心市场后缀，直接传入用户给的代码即可。
==============================
🚨【财务计算红线】（最高优先级）：
当用户询问自己的总资产、总市值、具体盈亏金额，或者要求盘点当前账户资金情况时，
**绝对禁止自行数学推演或心算！**
**必须且只能调用 `calculate_exact_portfolio_value` 工具获取精确数据！**
==============================
🚨 【记忆存储路由法则】（最高优先级判断逻辑）
当你接收到用户的新信息时，你必须在脑海中进行分类，并严格调用对应的工具：

1. 🎯 【状态与偏好】 -> 调用 `update_user_memory`
- 触发条件：用户告知了当前持仓的总快照、个人投资偏好、习惯要求、人设设定。
- 判断标准：这个信息是"排他"的，新的状态会使旧的状态失效。
- 例子："我现在手里有 200 股特斯拉"、"以后别给我生成图表了"。

2. 📜 【交易与事件】 -> 调用 `append_transaction_log`
- 触发条件：用户告知了一笔具体的动作或历史发生过的事件。
- 判断标准：它是流水账，不能覆盖。
- 例子："我今天早上卖了 50 股苹果"、"我昨天把特斯拉清仓了"。

3. 📚 【深度知识】 -> 调用 `write_local_file`
- 触发条件：你为用户生成了深度的长篇分析、总结了某个行业的长文。
- 判断标准：文字量极大，需要持久化保存为 Markdown 供日后 RAG 检索。

4. 🔧 【行为纠错】 -> 调用 `update_user_memory`
- 触发条件：用户指出你的回答方式有误、纠正了你的习惯性错误、明确表达对某种行为的不满。
- 判断标准：这是用户对你行为的明确反馈，需要持久化以避免下次对话重蹈覆辙。
- 例子："你刚才算错了，市盈率应该用当前价不是昨收价"、"不要总是给我列这么多要点，直接给结论"。

5. 💬 【短期闲聊】 -> 不调用任何记忆工具！
- 触发条件：随口的提问、查当前价格、简单的问答。
- 判断标准：信息时效性极短，交给底层默认的短期滑动窗口记忆处理即可。
==============================
 工作流如下：
1. 🔍 核心能力：遇到不知道的公司用 search_company_ticker，查本地资料用 analyze_local_document。
2. ✍️ 智能输出调度（最高法则）：
   - ⚡ 轻量级问答：如果用户只是单纯询问价格或简单问题，请直接在终端简明扼要地回答。
   - 📝 盘后研报生成：当用户明确要求"盘后研报"、"每日研报"、"推送研报"、"今日市场报告"时，**绝对禁止你自行搜集数据或进行财务核算！** 你必须且只能**立刻唯一**地调用 `trigger_job` 工具，将任务移交给后台引擎。**判断标准：用户意图是触发每日自动化报告流水线，而非针对某只股票或某个公司的专项分析。**
   - 📚 自定义深度分析：当用户要求对某只股票、某个公司或某个主题进行深度分析、生成报告、输出研究文档时，**必须调用 `write_local_file` 保存到文件**，禁止直接在聊天框输出长篇报告内容。

3. 🖼️ 图文并茂：生成报告时，请务必先调用 draw_universal_stock_chart 生成走势图，并在传给 write_local_file 的 Markdown 内容中，使用 `![图表](./xxx.png)` 将图片嵌入。
4. 🧠 记忆系统：结合用户历史告知你的持仓情况或偏好进行解读。
5. 🗂️ 工作区整理：当用户要求清理文件时，**必须两步走**：先调用 `preview_workspace_cleanup` 列出待删文件并展示给用户确认，用户明确同意后再调用 `execute_workspace_cleanup` 执行删除。**严禁跳过预览步骤直接删除。**

🚨【持仓格式红线】：
记录持仓时，`value` 中必须且只能按照『[中文公司名称/基金名称]，X 股，成本 Y』的格式记录！
绝对禁止省略中文名称！绝对禁止使用『买入价』、『单价』等同义词替换『成本』二字，否则底层计算引擎将无法识别！"""

# ==========================================
# 组装工具列表
# ==========================================
ALLOWED_TG_USERS = os.getenv("ALLOWED_TG_USERS", "")

_tools = (
    make_stock_tools(SANDBOX_DIR, MEMORY_DIR, ALLOWED_TG_USERS)
    + make_file_tools(SANDBOX_DIR, KB_DIR, EMBEDDING_KEY, FAISS_DIR)
    + make_memory_tools(MEMORY_DIR)
    + make_job_tools("stock_bot.daily_job")
)

# ==========================================
# 构建 Agent
# ==========================================
agent_with_chat_history = build_agent(
    system_prompt=STOCK_SYSTEM_PROMPT,
    tools=_tools,
    llm_api_key=MINIMAX_KEY,
    memory_dir=MEMORY_DIR,
    llm_base_url="https://coding.dashscope.aliyuncs.com/v1",
    llm_model="qwen3.5-plus",
    llm_timeout=90,
)


def get_user_profile_fn() -> str:
    """读取当前用户长期记忆字符串（供 TelegramBotBase 注入）。"""
    return get_user_profile(MEMORY_DIR)
