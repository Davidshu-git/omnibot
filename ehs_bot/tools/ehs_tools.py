"""
EHS 领域专属工具工厂 - 供 EHSBot Agent 注册使用。

通过 make_ehs_tools() 工厂函数生成绑定了运行时目录的工具列表。
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from filelock import FileLock
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# ==========================================
# 作业许可证标准检查项数据（内置，无需外部依赖）
# ==========================================
_WORK_PERMIT_CHECKLISTS: dict[str, list[str]] = {
    "动火作业": [
        "作业区域内可燃物已清理或有效隔离",
        "作业半径 5m 内无可燃气体、易燃液体",
        "可燃气体检测仪检测合格（LEL < 10%）",
        "消防灭火器/水源已就位并确认有效",
        "动火点周边排水沟、地沟已覆盖防火毯",
        "作业人员已取得动火作业资质证书",
        "监火人已到位且了解应急撤离路线",
        "动火许可证已由授权人员签发",
        "邻近区域已挂警示牌/警戒线",
        "通风措施已落实（密闭空间须机械通风）",
    ],
    "高处作业": [
        "作业平台/脚手架已验收合格并挂牌",
        "安全网已张挂，规格符合要求",
        "作业人员已系好双大钩全身式安全带",
        "安全带挂点承载力 ≥ 22.2 kN 已确认",
        "作业人员已通过高处作业安全培训",
        "工具已系防坠绳，作业区下方已隔离",
        "天气确认：风速 < 10.8 m/s，无雷雨",
        "上下通道已清理，不得攀爬模板/脚手架斜撑",
        "夜间作业：照明 ≥ 50 lx，安全背心已穿戴",
        "高处作业许可证已签发并张贴",
    ],
    "有限空间": [
        "氧含量检测：19.5%–23.5%（合格区间）",
        "可燃气体检测：LEL < 10%",
        "有毒气体检测（H₂S < 10 ppm，CO < 25 ppm）",
        "通风设备已开启，持续送风（不得用纯氧替代）",
        "作业人员已配备SCBA/长管呼吸器并试用",
        "监护人员已在入口处就位，不得擅自离开",
        "安全绳/逃生装备已配备且固定牢靠",
        "内外通讯联络方式已确认",
        "急救人员及装备待命",
        "有限空间作业许可证已签发",
    ],
    "临时用电": [
        "临时用电方案已审批并归档",
        "配电箱已安装漏电保护器（动作电流 ≤ 30 mA）",
        "电缆已架空或穿管保护，不得拖地",
        "用电设备外壳已可靠接地/接零",
        "开关箱遵循「一机一闸一保护」原则",
        "潮湿环境安全电压 ≤ 24 V（特别危险 ≤ 12 V）",
        "临时配电线路已做绝缘摇测并合格",
        "作业人员已取得电工操作证（相应等级）",
        "临时用电许可证已签发",
    ],
    "吊装作业": [
        "吊装方案已由责任工程师审批",
        "吊车/行车已完成日常检查且在检验有效期内",
        "吊索具（钢丝绳/卸扣）已目视检查，无断丝/磨损超标",
        "吊点位置已计算确认，重心稳定",
        "起吊载荷 ≤ 额定载荷的 80%",
        "吊装区域已清场，警戒线已拉设",
        "指挥人员已统一手势/对讲频道",
        "试吊高度 ≤ 20 cm，停顿 5 min 确认稳定",
        "风速确认：> 6 级（13.9 m/s）停止作业",
        "吊装作业票已签发",
    ],
}


def make_ehs_tools(
    sandbox_dir: Path,
    memory_dir: Path,
) -> list:
    """
    创建 EHS 领域专属工具列表。

    Args:
        sandbox_dir: Agent 工作区目录（报告输出路径）
        memory_dir:  记忆目录（incident_log.jsonl 存储路径）
    """
    incident_log_file = memory_dir / "incident_log.jsonl"
    incident_log_lock = memory_dir / "incident_log.jsonl.lock"

    @tool
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    )
    def search_ehs_regulation(query: str) -> str:
        """
        🔍 联网搜索 EHS 法规、国家标准（GB/T）、ISO 标准及行业解读。
        当用户询问某条法规要求、标准条款、合规要点时必须先调用此工具获取最新信息，
        不要依赖训练数据中可能过时的法规内容。

        Args:
            query: 搜索关键词，例如 "ISO 45001 危险源辨识要求" 或 "GBZ 2.1 职业接触限值"
        """
        import requests
        try:
            from ddgs import DDGS
            ddgs = DDGS()
            results = ddgs.text(
                f"EHS 安全 {query} 法规标准",
                max_results=4,
            )
            if not results:
                return f"未找到关于 '{query}' 的相关法规或标准信息，建议直接查阅官方数据库。"
            formatted = []
            for r in results:
                formatted.append(f"【{r.get('title', '')}】\n{r.get('body', '')}\n来源：{r.get('href', '')}")
            return "\n\n".join(formatted)
        except requests.exceptions.Timeout:
            return f"联网搜索超时：查询 '{query}' 时无响应，请稍后重试。"
        except requests.exceptions.ConnectionError:
            return "网络连接失败：无法访问搜索服务，请检查网络。"
        except Exception as e:
            return f"搜索出错：{type(e).__name__} - {str(e)}"

    @tool
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    )
    def query_chemical_ghs_info(chemical: str) -> str:
        """
        ⚗️ 查询化学品 GHS 危害信息（危险类别、防护措施、应急处置）。
        支持按化学品名称（中英文）或 CAS 号查询。
        当用户询问某种化学品的危险性、储存要求、泄漏处置时必须调用此工具。

        Args:
            chemical: 化学品名称或 CAS 号，例如 "苯" 或 "71-43-2" 或 "Toluene"
        """
        import requests
        try:
            from ddgs import DDGS
            ddgs = DDGS()
            results = ddgs.text(
                f"{chemical} GHS SDS 危害分类 安全数据表 MSDS",
                max_results=4,
            )
            if not results:
                return f"未找到 '{chemical}' 的 GHS 信息，建议查阅国家化学品登记中心或 ECHA 数据库。"
            formatted = []
            for r in results:
                formatted.append(f"【{r.get('title', '')}】\n{r.get('body', '')}\n来源：{r.get('href', '')}")
            return "\n\n".join(formatted)
        except requests.exceptions.Timeout:
            return f"查询超时：'{chemical}' 信息获取失败，请稍后重试。"
        except requests.exceptions.ConnectionError:
            return "网络连接失败：无法访问查询服务，请检查网络。"
        except Exception as e:
            return f"查询出错：{type(e).__name__} - {str(e)}"

    @tool
    def log_incident(
        title: str,
        level: str,
        location: str,
        description: str,
        corrective_action: str = "",
    ) -> str:
        """
        📋 记录安全隐患或事故到本地日志（持久化追加）。
        当用户告知发现了隐患、发生了事故、完成了现场检查需要归档时调用此工具。

        Args:
            title:             隐患/事故简要标题（如 "配电箱门未关闭"）
            level:             严重等级，必须为以下之一：
                               "一般隐患" | "重大隐患" | "未遂事故" | "轻伤" | "重伤" | "死亡"
            location:          发生地点（如 "1号车间电气室"）
            description:       详细描述（现象、根本原因分析）
            corrective_action: 整改措施及期限（可选，整改完成后可补录）
        """
        valid_levels = {"一般隐患", "重大隐患", "未遂事故", "轻伤", "重伤", "死亡"}
        if level not in valid_levels:
            return f"❌ 参数错误：level 必须为 {valid_levels} 之一，实际收到 '{level}'"

        record = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S"),
            "timestamp": datetime.now().isoformat(),
            "title": title,
            "level": level,
            "location": location,
            "description": description,
            "corrective_action": corrective_action,
            "status": "待整改" if not corrective_action else "已整改",
        }

        try:
            with FileLock(incident_log_lock, timeout=5):
                incident_log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(incident_log_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except TimeoutError:
            return "❌ 写入失败：文件锁超时，请稍后重试。"
        except OSError as e:
            return f"❌ 写入失败：{e}"

        return (
            f"✅ 记录已归档（ID: {record['id']}）\n"
            f"  标题：{title}\n"
            f"  等级：{level}  地点：{location}\n"
            f"  状态：{record['status']}"
        )

    @tool
    def query_incident_log(
        days: int = 30,
        level: str = "",
        keyword: str = "",
    ) -> str:
        """
        🔎 查询历史安全隐患/事故记录。
        当用户要求统计隐患数量、回顾历史事故、生成月度安全简报时调用此工具获取原始数据。

        Args:
            days:    查询最近几天的记录，0 表示查全部。默认 30 天。
            level:   按严重等级过滤（如 "重大隐患"），空字符串表示不过滤。
            keyword: 按标题或描述关键词过滤，空字符串表示不过滤。
        """
        if not incident_log_file.exists():
            return "当前隐患日志为空，尚无任何记录。"

        try:
            records = []
            with open(incident_log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            return f"❌ 读取日志失败：{e}"

        # 过滤
        cutoff = datetime.now() - timedelta(days=days) if days > 0 else None
        filtered = []
        for r in records:
            if cutoff:
                try:
                    if datetime.fromisoformat(r["timestamp"]) < cutoff:
                        continue
                except (ValueError, KeyError):
                    pass
            if level and r.get("level") != level:
                continue
            if keyword and keyword not in r.get("title", "") and keyword not in r.get("description", ""):
                continue
            filtered.append(r)

        if not filtered:
            return "未找到符合条件的隐患记录。"

        # 统计摘要
        level_counts: dict[str, int] = {}
        for r in filtered:
            lvl = r.get("level", "未知")
            level_counts[lvl] = level_counts.get(lvl, 0) + 1

        pending = sum(1 for r in filtered if r.get("status") == "待整改")
        summary = (
            f"📊 共找到 {len(filtered)} 条记录（待整改：{pending} 条）\n"
            f"等级分布：{json.dumps(level_counts, ensure_ascii=False)}\n\n"
        )

        lines = []
        for r in filtered[-20:]:  # 最多返回最近 20 条
            lines.append(
                f"[{r.get('timestamp', '')[:10]}] [{r.get('level', '')}] {r.get('title', '')}\n"
                f"  地点：{r.get('location', '')}  状态：{r.get('status', '')}\n"
                f"  描述：{r.get('description', '')[:100]}{'...' if len(r.get('description',''))>100 else ''}"
            )

        return summary + "\n".join(lines)

    @tool
    def get_work_permit_checklist(permit_type: str) -> str:
        """
        📝 获取标准作业许可证安全检查清单。
        在生成 JSA/作业许可证文档之前必须调用此工具，获取官方标准检查项后再填充到模板。
        支持的作业类型：动火作业、高处作业、有限空间、临时用电、吊装作业。

        Args:
            permit_type: 作业类型名称，必须为以下之一：
                         "动火作业" | "高处作业" | "有限空间" | "临时用电" | "吊装作业"
        """
        checklist = _WORK_PERMIT_CHECKLISTS.get(permit_type)
        if checklist is None:
            supported = "、".join(_WORK_PERMIT_CHECKLISTS.keys())
            return f"❌ 不支持的作业类型 '{permit_type}'，当前支持：{supported}"

        lines = [f"✅ 【{permit_type}】标准安全检查清单（共 {len(checklist)} 项）：\n"]
        for i, item in enumerate(checklist, 1):
            lines.append(f"  {i:2d}. □ {item}")
        lines.append(
            f"\n⚠️ 以上检查项须在作业开始前逐项确认并由相关责任人签字，"
            f"请将此清单写入 write_local_file 保存为正式作业许可证附件。"
        )
        return "\n".join(lines)

    return [
        search_ehs_regulation,
        query_chemical_ghs_info,
        log_incident,
        query_incident_log,
        get_work_permit_checklist,
    ]
