"""
沙箱文件 I/O 工具工厂 - 供各 bot 的 agent 注册使用。
通过工厂函数生成绑定了具体目录的 LangChain 工具列表。
"""
import os
import json
import time as _time
from pathlib import Path
from typing import Optional
from functools import lru_cache

from langchain_core.tools import tool
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import DashScopeEmbeddings

import logging

logger = logging.getLogger(__name__)

_DEFAULT_EXTENSIONS = {'.pdf', '.md', '.txt', '.csv'}
_MAX_FAISS_CACHE = 10


def _resolve_safe_path(file_path: str, base_dir: Path) -> tuple[Optional[Path], Optional[str]]:
    """
    将 file_path 解析为 base_dir 内的安全绝对路径。

    Returns:
        (resolved_path, None)   — 路径合法
        (None, error_message)   — 路径越权
    """
    target = (base_dir / file_path).resolve()
    if not target.is_relative_to(base_dir):
        return None, f"❌ 安全拦截：路径 '{file_path}' 超出允许范围，已被系统拒绝。"
    return target, None


@lru_cache(maxsize=_MAX_FAISS_CACHE)
def _get_or_build_vectorstore(file_name: str, target_path_str: str, current_mtime: float,
                               embedding_key: str, faiss_dir_str: str):
    """L1(lru_cache) / L2(硬盘) / L3(重建) 三级向量库缓存引擎"""
    faiss_dir = Path(faiss_dir_str)
    doc_cache_dir = faiss_dir / f"{file_name}_vstore"
    meta_file = doc_cache_dir / "meta.json"

    embeddings = DashScopeEmbeddings(
        dashscope_api_key=embedding_key,
        model="text-embedding-v3",
    )

    if doc_cache_dir.exists() and meta_file.exists():
        try:
            with open(meta_file, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            if meta.get("mtime") == current_mtime:
                return FAISS.load_local(str(doc_cache_dir), embeddings,
                                        allow_dangerous_deserialization=True)
        except (json.JSONDecodeError, TypeError, Exception):
            pass

    target_path = Path(target_path_str)
    ext = target_path.suffix.lower()
    if ext == '.pdf':
        loader = PyPDFLoader(target_path_str)
    elif ext in ['.md', '.txt', '.csv']:
        loader = TextLoader(target_path_str, encoding='utf-8')
    else:
        raise ValueError(f"不支持的文件格式：{ext}")

    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    splits = [s for s in splitter.split_documents(docs) if s.page_content.strip()]

    if not splits:
        raise ValueError(f"文件 {file_name} 内容为空或无法提取有效文本")

    vectorstore = FAISS.from_documents(splits, embeddings)
    doc_cache_dir.mkdir(parents=True, exist_ok=True)
    vectorstore.save_local(str(doc_cache_dir))
    with open(meta_file, 'w', encoding='utf-8') as f:
        json.dump({"mtime": current_mtime, "file_name": file_name}, f)

    return vectorstore


def make_file_tools(sandbox_dir: Path, kb_dir: Path,
                    embedding_key: str, faiss_dir: Path,
                    allowed_extensions: set = None) -> list:
    """
    创建绑定了具体目录的文件 I/O 工具列表。

    Args:
        sandbox_dir: Agent 沙箱目录（读写报告用）
        kb_dir: 知识库目录（RAG 用）
        embedding_key: DashScope 向量化 API Key
        faiss_dir: FAISS 硬盘缓存目录
        allowed_extensions: 知识库允许的文件后缀，默认 {'.pdf','.md','.txt','.csv'}
    """
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    kb_dir.mkdir(parents=True, exist_ok=True)
    faiss_dir.mkdir(parents=True, exist_ok=True)

    if allowed_extensions is None:
        allowed_extensions = _DEFAULT_EXTENSIONS

    faiss_dir_str = str(faiss_dir)

    @tool
    def read_local_file(file_path: str) -> str:
        """
        当需要读取本地沙箱中的文件内容（如之前生成的报告）时调用此工具。
        输入参数为沙箱内的文件名或相对路径（例如：'report.md'）。
        注意：出于安全限制，你只能读取沙箱(agent_workspace)内的文件。
        """
        try:
            target_path, err = _resolve_safe_path(file_path, sandbox_dir)
            if err:
                return err
            if not target_path.exists():
                return f"❌ 找不到文件: {target_path.name}"
            with open(target_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return f"文件 {target_path.name} 的内容是:\n{content}"
        except PermissionError:
            return f"❌ 权限不足：无法读取文件"
        except UnicodeDecodeError:
            return f"❌ 编码错误：不是有效的 UTF-8 文件"
        except Exception as e:
            return f"读取文件出错：{type(e).__name__} - {str(e)}"

    @tool
    def write_local_file(file_path: str, content: str) -> str:
        """
        🚨【强制交付通道】：
        当你被要求"写报告"、"生成分析"、"保存到本地"时，**绝对禁止**在聊天窗口直接输出 Markdown 文本！
        你必须且只能调用此工具，将完整排版好的 Markdown 内容作为 `content` 参数传入。
        输入参数 file_path 为目标文件名（例如：'report.md'）。
        """
        try:
            target_path, err = _resolve_safe_path(file_path, sandbox_dir)
            if err:
                return err
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return f"✅ 成功！文件已安全写入沙箱: {target_path}"
        except PermissionError:
            return f"❌ 权限不足：无法写入文件"
        except OSError as e:
            return f"❌ 系统错误：{type(e).__name__} - {str(e)}"
        except Exception as e:
            return f"写入文件出错：{type(e).__name__} - {str(e)}"

    @tool
    def list_kb_files() -> str:
        """
        当用户让你从知识库搜索，或者你不知道具体文件名时，必须先调用此工具！
        它会返回知识库文件夹下所有可用的文件列表。
        """
        try:
            files = [f.name for f in kb_dir.iterdir()
                     if f.is_file() and f.suffix.lower() in allowed_extensions]
            if not files:
                return "当前知识库文件夹为空，没有找到任何支持的文件。"
            return "知识库中当前有以下文件可以读取:\n" + "\n".join(files)
        except Exception as e:
            return f"读取目录出错：{type(e).__name__} - {str(e)}"

    @tool
    def analyze_local_document(file_name: str, query: str) -> str:
        """
        分析知识库中的文档（支持 PDF、Markdown、TXT 等）并回答问题。
        输入参数 file_name 只需要提供文件名（例如 'report.pdf'），不要提供完整路径！
        """
        try:
            target_path, err = _resolve_safe_path(file_name, kb_dir)
            if err:
                return err
            if not target_path.exists():
                return f"❌ 找不到文件：{file_name}。请先使用 list_kb_files 工具查看当前有哪些文件。"

            current_mtime = os.path.getmtime(target_path)
            vectorstore = _get_or_build_vectorstore(
                file_name, str(target_path), current_mtime, embedding_key, faiss_dir_str
            )
            retriever = vectorstore.as_retriever(search_kwargs={"k": 3})
            relevant_docs = retriever.invoke(query)
            context = "\n---\n".join([doc.page_content for doc in relevant_docs])
            return f"✅ 从文档 {file_name} 中检索到以下核心信息：\n{context}\n\n请根据以上数据回答。"
        except json.JSONDecodeError:
            return f"❌ 文档元数据损坏：JSONDecodeError"
        except ValueError as e:
            return f"❌ 数据错误：{e}"
        except RuntimeError as e:
            return f"❌ 系统错误：{e}"
        except Exception as e:
            return f"解析或检索文档出错：{type(e).__name__} - {str(e)}"

    @tool
    def preview_workspace_cleanup(older_than_days: int = 0, file_extensions: str = "") -> str:
        """
        扫描 agent_workspace 并列出符合条件的待删文件，不执行任何删除操作。
        用户确认列表后，再调用 execute_workspace_cleanup 执行删除。
        注意：table_render_*.png 为系统自动清理的临时文件，不在扫描范围内。

        Args:
            older_than_days: 仅列出修改时间超过指定天数的文件，0 表示不限制。
            file_extensions: 仅列出指定后缀的文件，多个后缀用逗号分隔（如 "md,pdf"），空字符串表示不限制。
        """
        now = _time.time()
        ext_filter = [e.strip().lstrip('.').lower()
                      for e in file_extensions.split(',') if e.strip()] if file_extensions else []

        candidates = []
        for f in sandbox_dir.iterdir():
            if not f.is_file():
                continue
            if f.name.startswith("table_render_") and f.suffix.lower() == ".png":
                continue
            stat = f.stat()
            age_days = (now - stat.st_mtime) / 86400
            ext = f.suffix.lstrip('.').lower()
            if older_than_days > 0 and age_days < older_than_days:
                continue
            if ext_filter and ext not in ext_filter:
                continue
            candidates.append({
                "name": f.name,
                "size_mb": round(stat.st_size / (1024 * 1024), 3),
                "age_days": round(age_days, 1),
            })

        if not candidates:
            return "✅ 未找到符合条件的文件，无需清理。"

        candidates.sort(key=lambda x: x["age_days"], reverse=True)
        total_mb = sum(c["size_mb"] for c in candidates)
        lines = [f"📋 找到 {len(candidates)} 个待删文件，共 {total_mb:.2f} MB：\n"]
        for c in candidates:
            lines.append(f"  • {c['name']}  ({c['size_mb']} MB, {c['age_days']} 天前)")
        lines.append(f"\n⚠️ 请向用户展示以上列表并询问是否确认删除。确认后调用 execute_workspace_cleanup 执行。")
        return "\n".join(lines)

    @tool
    def execute_workspace_cleanup(filenames: str) -> str:
        """
        删除 agent_workspace 中指定的文件。必须在用户明确确认后才能调用。

        Args:
            filenames: 要删除的文件名列表，用逗号分隔（仅文件名，不含路径）。
        """
        names = [n.strip() for n in filenames.split(',') if n.strip()]
        if not names:
            return "❌ 未提供任何文件名。"

        deleted_labels, deleted_sizes, failed = [], [], []
        for name in names:
            target, err = _resolve_safe_path(name, sandbox_dir)
            if err:
                failed.append(f"{name}（路径越权，已拦截）")
                continue
            if not target.exists():
                failed.append(f"{name}（文件不存在）")
                continue
            try:
                size_mb = round(target.stat().st_size / (1024 * 1024), 3)
                target.unlink()
                deleted_labels.append(f"{name} ({size_mb} MB)")
                deleted_sizes.append(size_mb)
            except OSError as e:
                failed.append(f"{name}（删除失败：{e}）")

        total_mb = sum(deleted_sizes)
        result = [f"✅ 成功删除 {len(deleted_labels)} 个文件，释放 {total_mb:.2f} MB："]
        result += [f"  • {d}" for d in deleted_labels]
        if failed:
            result += [f"\n⚠️ 以下文件未能删除："] + [f"  • {f}" for f in failed]
        return "\n".join(result)

    @tool
    def preview_kb_cleanup(older_than_days: int = 0, name_pattern: str = "", file_extensions: str = "") -> str:
        """
        扫描知识库并列出符合条件的待删文件，不执行任何删除操作。
        用户确认列表后，再调用 execute_kb_cleanup 执行删除。

        Args:
            older_than_days: 仅列出修改时间超过指定天数的文件，0 表示不限制。
            name_pattern: 仅列出文件名包含该关键词的文件（如 "盘后日报"），空字符串表示不限制。
            file_extensions: 仅列出指定后缀的文件，多个后缀用逗号分隔（如 "md,pdf"），空字符串表示不限制。
        """
        now = _time.time()
        ext_filter = [e.strip().lstrip('.').lower()
                      for e in file_extensions.split(',') if e.strip()] if file_extensions else []

        candidates = []
        try:
            for f in kb_dir.iterdir():
                if not f.is_file() or f.suffix.lower() not in allowed_extensions:
                    continue
                if name_pattern and name_pattern not in f.name:
                    continue
                stat = f.stat()
                age_days = (now - stat.st_mtime) / 86400
                ext = f.suffix.lstrip('.').lower()
                if older_than_days > 0 and age_days < older_than_days:
                    continue
                if ext_filter and ext not in ext_filter:
                    continue
                candidates.append({
                    "name": f.name,
                    "size_mb": round(stat.st_size / (1024 * 1024), 3),
                    "age_days": round(age_days, 1),
                })
        except Exception as e:
            return f"❌ 扫描知识库出错：{type(e).__name__} - {str(e)}"

        if not candidates:
            return "✅ 未找到符合条件的知识库文件，无需清理。"

        candidates.sort(key=lambda x: x["age_days"], reverse=True)
        total_mb = sum(c["size_mb"] for c in candidates)
        lines = [f"📋 找到 {len(candidates)} 个知识库文件，共 {total_mb:.2f} MB：\n"]
        for c in candidates:
            lines.append(f"  • {c['name']}  ({c['size_mb']} MB, {c['age_days']} 天前)")
        lines.append("\n⚠️ 请向用户展示以上列表并询问是否确认删除。确认后调用 execute_kb_cleanup 执行。")
        return "\n".join(lines)

    @tool
    def execute_kb_cleanup(filenames: str) -> str:
        """
        删除知识库中指定的文件，同时清理对应的 FAISS 向量缓存。必须在用户明确确认后才能调用。

        Args:
            filenames: 要删除的文件名列表，用逗号分隔（仅文件名，不含路径）。
        """
        import shutil
        names = [n.strip() for n in filenames.split(',') if n.strip()]
        if not names:
            return "❌ 未提供任何文件名。"

        deleted_labels, deleted_sizes, failed = [], [], []
        for name in names:
            target, err = _resolve_safe_path(name, kb_dir)
            if err:
                failed.append(f"{name}（路径越权，已拦截）")
                continue
            if not target.exists():
                failed.append(f"{name}（文件不存在）")
                continue
            try:
                size_mb = round(target.stat().st_size / (1024 * 1024), 3)
                target.unlink()
                cache_dir = faiss_dir / f"{name}_vstore"
                if cache_dir.exists():
                    shutil.rmtree(cache_dir)
                deleted_labels.append(f"{name} ({size_mb} MB)")
                deleted_sizes.append(size_mb)
            except OSError as e:
                failed.append(f"{name}（删除失败：{e}）")

        if deleted_labels:
            _get_or_build_vectorstore.cache_clear()

        total_mb = sum(deleted_sizes)
        result = [f"✅ 成功删除 {len(deleted_labels)} 个知识库文件，释放 {total_mb:.2f} MB："]
        result += [f"  • {d}" for d in deleted_labels]
        if failed:
            result += [f"\n⚠️ 以下文件未能删除："] + [f"  • {f}" for f in failed]
        return "\n".join(result)

    return [
        read_local_file,
        write_local_file,
        list_kb_files,
        analyze_local_document,
        preview_workspace_cleanup,
        execute_workspace_cleanup,
        preview_kb_cleanup,
        execute_kb_cleanup,
    ]
