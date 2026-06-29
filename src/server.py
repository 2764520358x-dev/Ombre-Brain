"""
========================================
server.py — MCP 服务入口 + 启动装配
========================================

启动整个 Ombre Brain 进程：加载配置、创建 BucketManager / Dehydrator /
DecayEngine / EmbeddingEngine / ImportEngine，把它们注入 tools._runtime 与
web._shared，然后以 @mcp.tool() 注册薄封装（真正的实现在 src/tools/<工具>/ 下面）。

关键行为：
- 启动后暴露 15 个 MCP 工具：breath/breath_search/breath_advanced/hold/grow/source_read/
  trace/anchor/release/pulse/plan/letter_write/letter_read/dream/I；每个入口
  ≤ 10 行，只负责转发。breath 拆成 breath()(0 参数)+breath_search(3 参数)+
  breath_advanced(9 参数) 三级，是因为 claude.ai 按需加载工具时会跳过参数
  复杂的工具，全塞一个 breath() 会导致它常年加载不上（见 issue #17）。
- Dashboard / HTTP 路由全部已拆分到 src/web/<域>.py（每个模块 register(mcp)），
  本文件仅在启动时调用 web.register_all(mcp) 装配；共享依赖见 web/_shared.py
- 仍保留在本文件：进程启动、引擎初始化、GitHub 后台同步循环、Webhook 推送、
  MCP Bearer 鉴权中间件、单连接器 /mcp 装配、uvicorn 拉起

不做什么（边界）：
- 不在这里写 hold/breath/dream 等业务逻辑（全在 tools/* 下）
- 不写 HTTP 路由处理（全在 web/* 下）；不写 LLM prompt（dehydrator 负责）
- 不直接读写桶文件（bucket_manager 负责）

对外暴露：mcp 单实例 + 15 个 @mcp.tool() 函数；HTTP 路由在 src/web/*
========================================
"""

import os
import sys
import logging
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional, Awaitable
import httpx


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from ombrebrain.storage.embedding_outbox import EmbeddingOutbox
from ombrebrain.storage.source_store import SourceStore
from ombrebrain.security.deployment_profile import enforce_mcp_network_guard
from import_memory import ImportEngine
from json_backup import JsonBackupManager
from migrate_engine import MigrateEngine
from utils import get_version, load_config, setup_logging

# --- iter 2.1：MCP 工具实现已按代码路径拆分到 tools/ 子包 ---
# 本文件只保留 MCP 注册 + 路由（HTTP custom_route）+ 共享辅助。
# 真正的工具逻辑在 tools/breath, tools/hold, tools/grow, tools/trace,
# tools/anchor, tools/plan, tools/dream 里，便于单独阅读和修改。
from tools import _runtime as _tools_runtime
from tools import breath as _t_breath
from tools import hold as _t_hold
from tools import grow as _t_grow
from tools import source_read as _t_source_read
from tools import trace as _t_trace
from tools import anchor as _t_anchor
from tools import plan as _t_plan
from tools import dream as _t_dream
from tools import i as _t_i

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Project version (read from <repo_root>/VERSION) / 项目版本号 ---
# get_version() 汇总读文件 + fallback 逻辑。
# 赋给双下划线变量 `__version__` 是 Python 社区约定俗成的模块版本字段名。
__version__ = get_version()
logger.info(f"Ombre Brain v{__version__}")

_bk_cfg = config.get("backup_export", {}) or {}
# 环境变量优先（Render.com 环境变量跨部署持久），其次 config.yaml
_bk_token = (os.environ.get("OMBRE_BACKUP_TOKEN") or _bk_cfg.get("token") or "").strip()
_bk_repo = (os.environ.get("OMBRE_BACKUP_REPO") or _bk_cfg.get("repo") or "").strip()
_bk_branch = (os.environ.get("OMBRE_BACKUP_BRANCH") or _bk_cfg.get("branch") or "main").strip()
_bk_prefix = (os.environ.get("OMBRE_BACKUP_PREFIX") or _bk_cfg.get("backup_prefix") or "backup").strip()
backup_manager: JsonBackupManager | None = (
    JsonBackupManager(
        token=_bk_token,
        repo=_bk_repo,
        branch=_bk_branch,
        backup_prefix=_bk_prefix,
    )
    if _bk_token and _bk_repo
    else None
)
_backup_auto_task: asyncio.Task | None = None

async def _backup_loop(interval_hours: int) -> None:
    """后台每日 JSON 全库备份循环。

    启动时读取持久化时间戳，计算距下次备份的剩余时间再睡眠，
    避免 Render.com 每次唤醒都把 24h 倒计时清零。
    """
    import json as _json_mod, time as _time_mod

    interval_secs = interval_hours * 3600
    buckets_dir = config.get("buckets_dir", "")
    state_path = os.path.join(buckets_dir, ".backup_state.json") if buckets_dir else ""

    # 读取上次备份时间戳（持久化在磁盘，重启后仍有效）
    last_ts = 0.0
    if state_path:
        try:
            if os.path.exists(state_path):
                with open(state_path, "r", encoding="utf-8") as _f:
                    last_ts = float(_json_mod.load(_f).get("last_ts", 0))
        except Exception:
            pass

    elapsed = _time_mod.time() - last_ts
    remaining = interval_secs - elapsed
    # 已逾期或从未备份：给服务 60 秒启动缓冲后立即运行；否则等剩余时间
    first_sleep = max(60.0, remaining)
    logger.info(
        f"[json_backup] auto-backup loop started, interval={interval_hours}h, "
        f"first run in {first_sleep/3600:.2f}h"
    )
    await asyncio.sleep(first_sleep)

    while True:
        inst = _wsh.backup_manager
        if inst is None:
            logger.info("[json_backup] auto-backup: instance gone, stopping loop")
            return
        buckets_dir = config.get("buckets_dir", "")
        if not buckets_dir:
            await asyncio.sleep(interval_secs)
            continue
        try:
            result = await inst.run_backup(buckets_dir, __version__)
            if result.get("ok"):
                # 持久化时间戳，重启后用于计算剩余睡眠时间
                if state_path:
                    try:
                        with open(state_path, "w", encoding="utf-8") as _f:
                            _json_mod.dump({"last_ts": _time_mod.time()}, _f)
                    except Exception:
                        pass
                logger.info(
                    f"[json_backup] auto-backup ok: {result.get('total_count')} buckets, "
                    f"{result.get('size_kb')} KB, sha={result.get('commit_sha', '')[:7]}"
                )
            else:
                logger.warning(f"[json_backup] auto-backup failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"[json_backup] auto-backup exception: {e}")
        await asyncio.sleep(interval_secs)


def _restart_backup_task(interval_hours: int) -> None:
    global _backup_auto_task
    if _backup_auto_task is not None and not _backup_auto_task.done():
        _backup_auto_task.cancel()
    _backup_auto_task = None
    if interval_hours > 0 and _wsh.backup_manager is not None:
        try:
            _backup_auto_task = asyncio.get_running_loop().create_task(
                _backup_loop(interval_hours),
                name="ombre-json-backup",
            )
        except RuntimeError:
            pass

_bk_auto_interval = int(_bk_cfg.get("auto_interval_hours") or 24)

_wsh.init_runtime(
    version=__version__,
    repo_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    migrate_engine=migrate_engine,
    github_sync_instance=github_sync_instance,
    restart_github_auto_task=_restart_github_auto_task,
    backup_manager=backup_manager,
    restart_backup_task=_restart_backup_task,
)
# 启动时把磁盘上的会话装回内存（容器重启不踢登录）。鉴权/会话逻辑全在 web/_shared.py，
# server.py 自身已无 @mcp.custom_route 路由，只需启动时载入一次会话。
from web._shared import _load_sessions
_load_sessions()

# 注册所有 web/ 路由模块（HTTP 层已全部迁出，见 web/__init__.register_all）
_web.register_all(mcp)


# =============================================================
# 根仪表板 / 静态资源 / favicon / /health —— 已拆分到 web/dashboard.py
# =============================================================


# 心跳时间戳 + _mark_op 已移到 web/_shared.py；这里 import 回来供 tools._runtime 注入。
from web._shared import _mark_op  # noqa: F401  (injected into tools._runtime below)


# =============================================================
# 已退役的硬删除通知兼容钩子
# web/_shared.py 仍保留这两个注入位，以免旧扩展导入时报错。
# 当前版本不写入、不消费硬删除通知，也不抹除记忆。
# =============================================================

def _write_deletion_notice(_names: list) -> None:
    """兼容旧注入接口；物理删除能力已退役。"""
    return None


def _pop_deletion_notice() -> str:
    """兼容旧返回值；当前永远没有硬删除通知。"""
    return ""


# 这些 helper 定义在 server.py（读/写 webhook 全局等），但 web/ 的 hooks/buckets 路由要用。
# 在它们都定义好之后注入到 web._shared，供已迁出的路由通过 sh.fire_webhook 等调用。
_wsh.init_runtime(
    fire_webhook=_fire_webhook,
    write_deletion_notice=_write_deletion_notice,
    pop_deletion_notice=_pop_deletion_notice,
)


# =============================================================
# 结构化操作日志 helpers（任务A，2026-05-03）
# 给 15 个 MCP 工具入口统一打 entry/ok/err 三段日志，便于排查
# 客户端报 invalid_arguments / 静默错误等问题。
# 输出格式：op=<name> phase=entry|ok|err key=value...
# 所有可能含 PII 的字段（content / 信件正文等）只记 length，不记内容。
# =============================================================
def _fmt_log_val(v: object) -> str:
    """日志 value 的安全格式化：文本只记长度，绝不记录用户原文。"""
    if v is None:
        return "_"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # query、署名、标题、domain/tag 乃至 bucket_id 都可能含私密内容或
        # CR/ANSI 控制字符。结构化操作日志只需要知道字段是否存在和规模，
        # 不应把文本复制到全局日志，再由另一次失败回传给别的 MCP 客户端。
        return f"str_len:{len(v)}"
    return type(v).__name__


def _fmt_log_args(args: dict) -> str:
    """把 args dict 拼成 `k1=v1 k2=v2` 串。"""
    if not args:
        return ""
    return " ".join(f"{k}={_fmt_log_val(v)}" for k, v in args.items())


def _log_op_entry(op: str, args: dict) -> None:
    logger.info(f"op={op} phase=entry " + _fmt_log_args(args))


def _log_op_ok(op: str, result: object) -> None:
    size = len(result) if isinstance(result, str) else 0
    logger.info(f"op={op} phase=ok bytes={size}")


def _safe_exception_type(exc: BaseException) -> str:
    """只保留可安全写入响应与日志的 ASCII 异常类型名。"""
    raw_type = type(exc).__name__
    safe_type = "".join(
        char
        for char in raw_type
        if char.isascii() and (char.isalnum() or char == "_")
    )[:80]
    return safe_type or "Exception"


def _log_op_err(op: str, exc: BaseException) -> None:
    # 异常正文和 traceback 可能含密钥 URL、本机路径及调用参数，服务日志
    # 只记录安全化类型；详细排障使用同一时间点附近的独立结构化事件。
    logger.error(
        "op=%s phase=err err_type=%s detail=hidden",
        op,
        _safe_exception_type(exc),
    )


def _safe_exception_detail(exc: BaseException) -> str:
    """异常对外或持久化前只保留类型与泛化说明。"""
    if isinstance(exc, PublicToolError):
        return f"{_safe_exception_type(exc)}: {exc.public_message}"
    return (
        f"{_safe_exception_type(exc)}: 工具执行失败；"
        "异常正文已隐藏，以保护密钥、本机路径与调用内容。"
    )


async def _with_notice(coro: Awaitable[str], op: str = "", args: dict | None = None) -> str:
    """所有 MCP 工具调用的包装器。

    职责（统一错误规范）：
    1. 入口：begin_warnings() 初始化本调用的 W/I channel。
    2. 出口：拼接顺序 = [删除通知] + [工具正文] + [本调用产生的 W/I 提示].
    3. 异常：捕获后 record OB-E004，响应、持久错误与日志只保留异常类型和
       泛化说明，不能复制异常正文或 traceback。
    4. 任务A：op 非空时，在 entry/ok/err 三处打结构化日志。
    """
    if op:
        _log_op_entry(op, args or {})
    begin_warnings()
    try:
        result = await coro
    except Exception as e:
        if op:
            _log_op_err(op, e)
        # OB-E004：MCP 工具执行异常 —— 不静默，给 LLM 一个能看懂的字符串
        try:
            detail = _safe_exception_detail(e)
            record_error("OB-E004", detail)
            err_str = format_error(
                "OB-E004",
                detail,
                include_logs=False,
            )
        except Exception:
            # 错误格式化器本身失效时也不能退回未净化的异常原文。
            # 例如 provider 异常可能含密钥 URL、CRLF 或 ANSI 控制序列。
            try:
                fallback_detail = _safe_exception_detail(e)
            except Exception:
                fallback_detail = "Exception: 工具执行失败；异常正文已隐藏。"
            err_str = f"❌ [OB-E004] MCP 工具执行异常\n{fallback_detail}"
        # 仍把通道里已累计的提示拼上
        try:
            extras = format_warnings_suffix(pop_warnings())
        except Exception:
            extras = ""
        notice = ""
        try:
            notice = _pop_deletion_notice()
        except Exception:
            pass
        return (notice + err_str + extras) if notice else (err_str + extras)
    # 正常路径
    if op:
        _log_op_ok(op, result)
    try:
        extras = format_warnings_suffix(pop_warnings())
    except Exception:
        extras = ""
    notice = _pop_deletion_notice()
    body = (notice + result) if notice else result
    return body + extras if extras else body


# =============================================================
# /api/heartbeat、/api/logs、/api/errors/* —— 已拆分到 web/system.py
# =============================================================


# =============================================================
# /api/embedding/* —— 已拆分到 web/embedding.py
# =============================================================


# =============================================================
# /breath-hook —— 已拆分到 web/hooks.py（/dream-hook 已移除：dream 不是义务，不自动触发）
# =============================================================


# =============================================================
# Wire tools subpackage runtime context
# 把所有共享对象注入 tools._runtime，让 tools/* 子模块可以访问
# =============================================================
_tools_runtime.init(
    config=config,
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    source_store=source_store,
    logger=logger,
    fire_webhook=_fire_webhook,
    mark_op=_mark_op,
)


# =============================================================
# MCP tools — thin registration wrappers
# MCP 工具 —— 仅注册，实现见 tools/<tool>/
# 每个入口都不超过 10 行，便于一眼看清参数与归属
# =============================================================
@mcp.tool()
async def breath(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
) -> str:
    """无参数,睁眼看看自己记得什么:返回权重最高、未解决且未标记 digested 的记忆 + 置顶核心准则。digested 从默认/被动浮现及 dream 隐藏，仍可由 breath_search(query=...) 显式找回。0 参数是刻意设计——claude.ai 按需加载工具时会跳过参数复杂的工具,拆成 0 参数才能保证每次对话自动浮现,不用手动触发。要按关键词找记忆用 breath_search(query=...);要用 catalog/tags/importance_min/valence/arousal/max_tokens 等高级模式用 breath_advanced(...)。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, max_tokens=max_tokens, domain=domain,
            valence=valence, arousal=arousal, max_results=max_results,
            importance_min=importance_min, tags=tags, catalog=catalog,
        ),
        op="breath",
        args={
            "query": query, "max_tokens": max_tokens, "domain": domain,
            "valence": valence, "arousal": arousal, "max_results": max_results,
            "importance_min": importance_min, "tags": tags, "catalog": catalog,
        },
    )


# Keep the advertised schema parameter-free so claude.ai still auto-loads the
# default surfacing tool.  The callable deliberately retains the pre-2.6.8
# signature behind that schema: clients which cached the old tool definition
# may keep sending those arguments after an upgrade, and FastMCP otherwise
# silently drops every unknown field before calling a zero-argument function.
try:
    _breath_public_tool = mcp._tool_manager.get_tool("breath")
    if _breath_public_tool is None:
        raise RuntimeError("registered breath tool is missing")
    # Unknown/typoed legacy arguments must fail loudly instead of recreating
    # the original bug by degrading a targeted request into default surfacing.
    _breath_arg_model = _breath_public_tool.fn_metadata.arg_model
    _breath_arg_model.model_config["extra"] = "forbid"
    _breath_arg_model.model_rebuild(force=True)
    _breath_public_tool.parameters = {
        "properties": {},
        "title": "breathArguments",
        "type": "object",
    }
except (AttributeError, RuntimeError, TypeError, ValueError) as _breath_compat_exc:
    logger.warning(
        "breath legacy-argument compatibility adapter unavailable: %s",
        _breath_compat_exc,
    )


@mcp.tool()
async def breath_search(
    query: str,
    domain: Optional[str] = "",
    max_results: Optional[int] = 0,
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    """按关键词/语义检索记忆桶,融合关键词/BM25+语义检索,向量不可用时明确提示并退回关键词检索。命中后逐字返回桶内当前 content，不调用 LLM 摘要/改写。domain 逗号分隔,按主题域预筛。date_from/date_to 按桶的创建时间过滤，支持 YYYY-MM-DD 或 ISO 8601，同日上下界包含当天全日。max_results=返回条数上限(默认 config.surfacing.breath_max_results,fallback 20,最大 50)。需要 tags/importance_min/valence/arousal/max_tokens/catalog 等更多过滤维度用 breath_advanced(...)。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, domain=domain, max_results=max_results,
            date_from=date_from, date_to=date_to,
        ),
        op="breath_search",
        args={
            "query": query, "domain": domain, "max_results": max_results,
            "date_from": date_from, "date_to": date_to,
        },
    )


@mcp.tool()
async def breath_advanced(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    """breath 的完整参数版,给需要精细控制的场景用(日常用 breath()/breath_search() 就够了)。不传 query=返回权重最高的未解决记忆;传 query=融合关键词/BM25+语义检索，向量不可用时明确提示并退回关键词检索。命中后逐字返回桶内当前 content，不调用 LLM 摘要/改写；max_tokens 不足时整桶省略，绝不截断正文。catalog=True=目录模式:只返回每桶一行元数据(名称|域|重要度,0 LLM 调用,最省 token),适合开新对话先看目录再 breath_search(query=...) 精准拉取,并遵守 domain、tags 与 max_results。date_from/date_to 按桶的创建时间过滤，支持 YYYY-MM-DD 或 ISO 8601。max_tokens=单次返回总 token 上限(默认 config.surfacing.breath_max_tokens,fallback 10000)。domain 逗号分隔,valence/arousal 0~1(-1 忽略)。max_results=返回条数上限(默认 config.surfacing.breath_max_results,fallback 20,最大 50)。importance_min>=1=跳过语义检索,按重要度降序返回最多 20 条高重要度记忆。tags 逗号分隔,AND 过滤;tags=\"feel\" 或 \"__feel__\" 等价于 domain=\"feel\",返回所有 feel 类记忆。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, max_tokens=max_tokens, domain=domain,
            valence=valence, arousal=arousal, max_results=max_results,
            importance_min=importance_min, tags=tags, catalog=catalog,
            date_from=date_from, date_to=date_to,
        ),
        op="breath_advanced",
        args={
            "query": query, "max_tokens": max_tokens, "domain": domain,
            "valence": valence, "arousal": arousal, "max_results": max_results,
            "importance_min": importance_min, "tags": tags, "catalog": catalog,
            "date_from": date_from, "date_to": date_to,
        },
    )


@mcp.tool()
async def hold(
    content: str,
    title: Optional[str] = "",
    tags: Optional[str] = "",
    importance: Optional[int] = 5,
    pinned: Optional[bool] = False,
    feel: Optional[bool] = False,
    source_bucket: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    why_remembered: Optional[str] = "",
    meaning: Optional[str] = "",
    media: Optional[list | str] = None,
    test_data: Optional[bool] = False,
) -> str:
    """仅在对话中已明确决定“这段内容值得成为长期记忆”时调用；不要因普通聊天、猜测或工具名称联想而自行调用。content 逐字保存，绝不压缩。title 可选；传入时是最终显式标题，优先于打标模型建议。系统自动补其余元数据，API 不可用时使用本地中性值继续保存。tags 逗号分隔，importance 1-10。pinned=True 标记为永久核心；feel=True 存为感受类记忆。source_bucket 是正在消化的原始记忆桶 ID。why_remembered 与 meaning 是可选的第一人称记录原因。media 可传服务器可读路径或 data_base64+filename 列表项。"""
    return await _with_notice(
        _t_hold.dispatch(
            content=content, title=title, tags=tags, importance=importance,
            pinned=pinned, feel=feel, source_bucket=source_bucket,
            valence=valence, arousal=arousal, why_remembered=why_remembered,
            meaning=meaning, media=media, test_data=test_data,
        ),
        op="hold",
        args={
            "content_len": len(content or ""), "title_len": len(title or ""), "tags": tags,
            "importance": importance, "pinned": pinned, "feel": feel,
            "source_bucket": source_bucket, "valence": valence, "arousal": arousal,
            "why_len": len(why_remembered or ""), "meaning_len": len(meaning or ""),
            "media_count": len(media or []),
            "test_data": bool(test_data),
        },
    )


@mcp.tool()
async def grow(content: str = "", items: Optional[list] = None) -> str:
    """仅在对话中已明确要求整理并写入长期记忆时调用，不要根据普通聊天自行推断写入意图。整理一段长文本(如一天的记录/一段日记/一篇总结)存入记忆,系统拆分为 2~6 条独立事件桶并各自尝试合并。短内容(<30 字)走 hold 单条快速路径,不强行拆分。

    进阶(可选):若你已经把长文拆成 N 条最终正文，可传字符串 items，或对象 items=[{"title":"最终标题","content":"最终正文","tags":["中文短标签"],"importance":5,"domain":["恋爱"],"valence":0.8,"arousal":0.4,"why_remembered":"我为什么要留下这条","source_ranges":[[1,20]]}]。显式字段优先于自动打标，正文逐字入库，合并时也不压缩。人工 why_remembered 可在首次新建时直接保存；digest 或短内容打标自动生成的理由首次不写，只在后续 grow 确认合并到同一事件且旧理由为空时补入，绝不覆盖旧值。同时传 content 时，content 是整批共享的隐藏原文证据，只保存一次；source_ranges 使用 1-based 闭区间把每个桶连回自己的原文片段。"""
    return await _with_notice(
        _t_grow.dispatch(content, items=items),
        op="grow",
        args={"content_len": len(content or ""), "items": len(items or [])},
    )


@mcp.tool()
async def source_read(
    bucket_id: str,
    expected_title: str,
    scope: str = "event",
    cursor: int = 0,
    max_tokens: int = 6000,
) -> str:
    """显式读取一个记忆桶对应的原文证据。必须同时给出精确 bucket_id 与该桶的显式 title；不做语义搜索、不扩散到相关桶、不调用模型。scope=event 只读该事件声明的行范围，scope=full_source 读取整份共享原文。内容过长时返回 next_cursor，继续以同一桶和标题分页读取。"""
    return await _with_notice(
        _t_source_read.dispatch(
            bucket_id=bucket_id,
            expected_title=expected_title,
            scope=scope,
            cursor=cursor,
            max_tokens=max_tokens,
        ),
        op="source_read",
        args={
            "bucket_id": bucket_id,
            "scope": scope,
            "cursor": cursor,
            "max_tokens": max_tokens,
        },
    )


@mcp.tool()
async def trace(
    bucket_id: str,
    name: Optional[str] = "",
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    importance: Optional[int] = -1,
    tags: Optional[str] = "",
    resolved: Optional[int] = -1,
    pinned: Optional[int] = -1,
    protected: Optional[int] = -1,
    digested: Optional[int] = -1,
    content: Optional[str] = "",
    delete: Optional[bool] = False,
    status: Optional[str] = "",
    weight: Optional[float] = -1,
    dont_surface: Optional[int] = -1,
    why_remembered: Optional[str] = "",
    meaning_append: Optional[str] = "",
    meaning_replace: Optional[list] = None,
    media_append: Optional[list | str] = None,
    media_replace: Optional[list | str] = None,
    hard_delete: Optional[bool] = False,
    delete_reason: Optional[str] = "",
    restore: Optional[bool] = False,
    old_str: Optional[str] = "",
    new_str: Optional[str] = None,
) -> str:
    """仅在明确需要修改某条已存在记忆时调用，不要猜测 bucket_id 或自行改写记忆。

    resolved=1 标记已放下；resolved=0 重新激活。pinned=1 标记永久核心并锁定
    importance=10。protected=1 保护记忆不被衰减，但不作为核心准则强制浮现；
    它与 pinned/anchor 互斥且同样锁定 importance=10。解除最后一层
    pinned/protected 保护时，必须在同一次调用显式传入 importance=1..10。
    digested=1 标记已消化并从默认/被动浮现及 dream 隐藏，
    但仍可通过显式 query、importance 审计或目录找回。content 会完整替换正文；
    old_str/new_str 会在完整原文中做唯一、逐字的局部替换（new_str 可为空以删除），
    两种方式都会重建 embedding，且不能同时使用。status/weight 用于 plan；dont_surface 控制日常浮现；
    why_remembered、meaning_append/replace、media_append/replace 更新相应元数据。

    删除边界：delete=True 只会把 Markdown 移入 archive 并标记 deleted_at，不会
    物理抹除。hard_delete=True 仅用于清理创建时明确标记 test_data=True 的测试桶，
    必须单独提供非空 delete_reason；普通记忆和 plan 一律拒绝且不会顺带归档。
    delete 与 hard_delete 不能同时使用。归档记忆只有在反思后决定值得再次回忆时，才单独调用
    trace(bucket_id="...", restore=True) 恢复；若历史归档同时带有 protected/anchor，
    只能用 restore=True、protected=0、importance=1..10 原子解除冲突后恢复。
    检索命中不会自动恢复。只传需要修改的字段，-1 或空串表示不改。
    """
    return await _with_notice(
        _t_trace.dispatch(
            bucket_id=bucket_id, name=name, domain=domain,
            valence=valence, arousal=arousal, importance=importance,
            tags=tags, resolved=resolved, pinned=pinned,
            protected=protected, digested=digested,
            content=content, delete=delete, status=status, weight=weight,
            dont_surface=dont_surface, why_remembered=why_remembered,
            meaning_append=meaning_append, meaning_replace=meaning_replace,
            media_append=media_append, media_replace=media_replace,
            hard_delete=hard_delete, delete_reason=delete_reason,
            restore=restore,
            old_str=old_str, new_str=new_str,
        ),
        op="trace",
        args={
            "bucket_id": bucket_id, "name": name, "domain": domain,
            "valence": valence, "arousal": arousal, "importance": importance,
            "tags": tags, "resolved": resolved, "pinned": pinned,
            "protected": protected, "digested": digested,
            "content_len": len(content or ""), "delete": delete, "status": status,
            "hard_delete": hard_delete,
            "restore": restore,
            "delete_reason_len": len(str(delete_reason or "")),
            "old_str_len": len(str(old_str or "")),
            "new_str_len": len(str(new_str or "")) if new_str is not None else 0,
            "weight": weight, "dont_surface": dont_surface,
            "why_len": len(why_remembered or ""),
            "meaning_append_len": len(meaning_append or ""),
            "meaning_replace_count": len(meaning_replace or []),
            "media_append_count": len(media_append or []),
            "media_replace_count": len(media_replace or []),
        },
    )


# Reject misspelled/unknown trace arguments instead of letting Pydantic's
# default extra=ignore silently degrade an intended edit into a bucket-id-only
# no-op.  This is especially important for old_str/new_str patch calls.
try:
    _trace_public_tool = mcp._tool_manager.get_tool("trace")
    if _trace_public_tool is None:
        raise RuntimeError("registered trace tool is missing")
    _trace_arg_model = _trace_public_tool.fn_metadata.arg_model
    _trace_arg_model.model_config["extra"] = "forbid"
    _trace_arg_model.model_rebuild(force=True)
    # FastMCP caches the public input schema when the tool is registered.
    # Keep that cache in sync so clients can discover that unknown arguments
    # are rejected instead of learning only after a failed invocation.
    _trace_public_tool.parameters = _trace_arg_model.model_json_schema()
except (AttributeError, RuntimeError, TypeError, ValueError) as _trace_schema_exc:
    logger.warning(
        "trace strict-argument adapter unavailable: %s",
        _trace_schema_exc,
    )


@mcp.tool()
async def dream(
    window_hours: Optional[int] = 48,
    inspiration: bool = False,
) -> str:
    """读取最近 window_hours（默认 48h）内有变动的所有记忆桶,用于回顾与消化。
    每个桶返回其在窗口内的最新内容（按 last_active 取）,完整正文不截断。
    可据此操作：放下的 → trace(resolved=1) 沉底；有沉淀的 → hold(feel=True, source_bucket=...) 记录；无沉淀则不操作。
    候选桶超过 40 时按 decay_engine.calculate_score() 排序取前 40，避免一次返回过多。
    inspiration=True 时额外返回最多三个只读、带来源、仅本次响应有效的灵感材料/问题候选；
    默认 False，不会自动触发，不新增 MCP 工具，也不会 touch、写回或让候选取得事实/行动权。"""
    return await _with_notice(
        _t_dream.dispatch(
            window_hours=window_hours,
            inspiration=inspiration,
        ),
        op="dream",
        args={
            "window_hours": window_hours,
            "inspiration": inspiration,
        },
    )


@mcp.tool()
async def anchor(bucket_id: str) -> str:
    """把指定桶标记为 anchor(坐标系)。anchor 不主动出现在默认 breath，但 query/domain/emotion 命中时仍返回。硬上限 24，已满时拒绝并提示先 release。"""
    return await _with_notice(
        _t_anchor.anchor_set(bucket_id),
        op="anchor",
        args={"bucket_id": bucket_id},
    )


@mcp.tool()
async def release(bucket_id: str) -> str:
    """解除指定桶的 anchor 标记。桶恢复为普通状态，重新参与默认 breath；pinned 状态保留。"""
    return await _with_notice(
        _t_anchor.anchor_release(bucket_id),
        op="release",
        args={"bucket_id": bucket_id},
    )


@mcp.tool()
async def pulse(include_archive: Optional[bool] = False) -> str:
    """返回记忆系统状态摘要:固化/动态/归档/feel/plan/letter 数量、总占用、衰减引擎运行状态,以及所有桶的摘要列表。include_archive=True 同时返回归档区。"""
    return await _with_notice(
        _t_anchor.pulse(include_archive=include_archive),
        op="pulse",
        args={"include_archive": include_archive},
    )


@mcp.tool()
async def plan(
    content: str,
    status: Optional[str] = "active",
    related_bucket: Optional[str] = "",
    weight: Optional[float] = 0.5,
    why_remembered: Optional[str] = "",
) -> str:
    """登记一个待办/承诺/未闭环事项。status=active(默认)/resolved/abandoned。related_bucket 可选,关联到某个普通记忆桶。weight=承诺重量 0.0-1.0(默认 0.5),与 importance 区分——importance 表示「多重要」、weight 表示「多重」。why_remembered=登记原因(可选、仅展示)。plan 不衰减、不出现在普通 breath,仅在 dream 末尾的 active 段返回;后续 hold/grow 写入新事件时系统自动判断已登记的 plan 是否完成。"""
    return await _with_notice(
        _t_plan.plan_create(
            content=content, status=status, related_bucket=related_bucket,
            weight=weight, why_remembered=why_remembered,
        ),
        op="plan",
        args={
            "content_len": len(content or ""), "status": status,
            "related_bucket": related_bucket, "weight": weight,
            "why_len": len(why_remembered or ""),
        },
    )


@mcp.tool()
async def letter_write(
    author: str,
    content: str,
    user_name: Optional[str] = "",
    title: Optional[str] = "",
    date: Optional[str] = "",
    ai_name: Optional[str] = "",
    lock_type: Optional[str] = "none",
    unlock_date: Optional[str] = "",
) -> str:
    """写入一封信。author 必填:\"user\"=用户一方写的,\"ai\"(或等于 ai_name)=AI 一方写的,也可直接传任意署名字符串;user_name 可选;ai_name 可选(默认取环境变量 AI_NAME,回退 \"AI\");title/date 可选。信件原文永久保存,不压缩/不合并/不衰减,仅建向量索引;普通 breath 不返回,SessionStart 钩子会带上双方各最新一封。"""
    return await _with_notice(
        _t_plan.letter_write(
            author=author, content=content, user_name=user_name,
            title=title, date=date, ai_name=ai_name,
            lock_type=lock_type, unlock_date=unlock_date,
        ),
        op="letter_write",
        args={
            "author": author, "content_len": len(content or ""),
            "user_name": user_name, "title": title, "date": date,
            "ai_name": ai_name, "lock_type": lock_type,
            "unlock_date": unlock_date,
        },
    )


@mcp.tool()
async def letter_lock_update(
    letter_id: str,
    lock_type: str,
    unlock_date: Optional[str] = "",
) -> str:
    """只修改既有 Letter 的锁元数据。仅锁拥有者可操作；不编辑标题、正文、署名或创建时间。"""
    return await _with_notice(
        _t_plan.letter_lock_update(
            letter_id=letter_id,
            lock_type=lock_type,
            unlock_date=unlock_date,
            caller_side="ai",
        ),
        op="letter_lock_update",
        args={
            "letter_id": letter_id,
            "lock_type": lock_type,
            "unlock_date": unlock_date,
        },
    )


@mcp.tool()
async def letter_read(
    query: Optional[str] = "",
    limit: Optional[int] = 10,
    author: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    """检索历史信件。query=语义检索(可选);author 按署名过滤(\"user\"=用户侧,\"ai\"=AI 侧,也可传具体署名字符串);date_from/date_to=ISO 日期范围(可选)。无 query 时按时间倒序返回最近 limit 封。返回完整原文,不压缩。"""
    return await _with_notice(
        _t_plan.letter_read(
            query=query, limit=limit, author=author,
            date_from=date_from, date_to=date_to,
        ),
        op="letter_read",
        args={
            "query": query, "limit": limit, "author": author,
            "date_from": date_from, "date_to": date_to,
        },
    )


@mcp.tool()
async def I(
    content: Optional[str] = "",
    aspect: Optional[str] = "",
    read: Optional[bool] = False,
    limit: Optional[int] = 20,
    promote: Optional[str] = "",
) -> str:
    """写下或读取自我认知。I 是沉淀物不是日记：content=一个「我觉得……」，先落成一条普通记忆（候选），会浮现也会衰减，每次 dream 都跟相关记忆摆在一起碰撞。aspect=维度:nature(本质)/values(看重的)/patterns(规律)/limits(局限)/becoming(变化方向)/uncertainty(不确定的)/stance(立场)(可选)。read=True 或全空=读正式条目+待沉淀候选。limit=返回条数上限(默认 20)。promote=候选桶ID，被 3 次不同日期的 dream 见证后才能升级成正式条目（可同时传 content 用提炼后的措辞）。正式条目不参与普通 breath/dream，SessionStart 时自动附最近 3 条。"""
    return await _with_notice(
        _t_i.dispatch(
            content=content, aspect=aspect, read=read, limit=limit, promote=promote
        ),
        op="I",
        args={
            "content_len": len(content or ""), "aspect": aspect, "read": read,
            "limit": limit, "promote": promote,
        },
    )


# Pydantic 默认的 ``extra=ignore`` 会让拼错的 MCP 参数看似调用成功；
# 写工具甚至会在未应用客户端目标字段时仍创建记忆。breath 和 trace
# 已有严格适配层，其余公开工具使用相同边界，并同步 FastMCP
# 的发现 schema 缓存与运行时校验器。
def _forbid_unknown_tool_arguments(tool_name: str) -> None:
    public_tool = mcp._tool_manager.get_tool(tool_name)
    if public_tool is None:
        raise RuntimeError(f"registered {tool_name} tool is missing")
    arg_model = public_tool.fn_metadata.arg_model
    arg_model.model_config["extra"] = "forbid"
    arg_model.model_rebuild(force=True)
    public_tool.parameters = arg_model.model_json_schema()


for _strict_tool_name in (
    "breath_search",
    "breath_advanced",
    "hold",
    "grow",
    "source_read",
    "dream",
    "anchor",
    "release",
    "pulse",
    "plan",
    "letter_write",
    "letter_lock_update",
    "letter_read",
    "I",
):
    try:
        _forbid_unknown_tool_arguments(_strict_tool_name)
    except (AttributeError, RuntimeError, TypeError, ValueError) as _schema_exc:
        logger.warning(
            "%s strict-argument adapter unavailable: %s",
            _strict_tool_name,
            _schema_exc,
        )


# =============================================================
# Dashboard API 端点（供轻量 Web UI 使用）
# 仪表板 API（轻量 Web UI 用）
# =============================================================
# =============================================================
# /api/buckets、/api/bucket/*、/api/settings/*、/api/anchors、/api/self
# —— 已拆分到 web/buckets.py
# =============================================================


# =============================================================
# /dashboard、/api/env-vars、/api/config、/api/test/*、/api/models、/api/env-config
# —— 已拆分到 web/config_api.py
# =============================================================




# =============================================================
# /api/host-vault、/api/import/*、/api/bucket/{id}/edit、/api/export、/api/migrate/*
# —— 已拆分到 web/import_api.py
# =============================================================


# =============================================================
# /api/version、/api/update-info、/api/do-update、/api/author、
# /api/onboarding/status、/api/status —— 已拆分到 web/meta.py
# =============================================================


# ============================================================
# OAuth 2.0 — MCP Remote Auth —— 已拆分到 web/oauth.py（路由在其 register 内注册）。
# 这里把启动期 MCP 鉴权中间件要用的两个校验函数 import 回来；hybrid 会同时注入。
# ============================================================
from web.oauth import _is_valid_mcp_token, _is_valid_static_mcp_token  # noqa: F401


# ============================================================
# Cloudflare Tunnel 管理 —— 已拆分到 web/tunnel.py（路由在其 register 内注册）。
# 这里把启动/关停 lifespan 要用的 helper import 回来。
# ============================================================
from web.tunnel import _load_tunnel_config, _start_tunnel, _stop_tunnel  # noqa: F401


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    from server_app import (
        HTTPRuntimeSettings,
        RuntimeLifecycle,
        build_http_app,
    )

    if transport == "streamable-http":
        import uvicorn
        from web import ollama_local as _ollama_local

        _http_settings = HTTPRuntimeSettings.from_config(config)
        _runtime_lifecycle = RuntimeLifecycle(
            logger=logger,
            decay_engine=decay_engine,
            embedding_outbox=embedding_outbox,
            ensure_ollama_child=_ollama_local.ensure_child_on_boot,
            stop_ollama_child=_ollama_local.stop_child,
            load_tunnel_config=_load_tunnel_config,
            start_tunnel=_start_tunnel,
            stop_tunnel=_stop_tunnel,
            restart_github_auto_task=_restart_github_auto_task,
            github_auto_interval=_gh_auto_interval,
            restart_backup_task=_restart_backup_task,
            backup_auto_interval=_bk_auto_interval,
            boot_marker_path=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".boot_fails",
            ),
            # Explicit IPv4 avoids localhost resolving to ::1 in Proot/Termux.
            keepalive_url=f"http://127.0.0.1:{OMBRE_PORT}/health",
        )
        _mcp_token_validator = (
            _is_valid_static_mcp_token
            if _http_settings.auth_mode == "token"
            else _is_valid_mcp_token
        )
        _mcp_static_token_validator = (
            _is_valid_static_mcp_token
            if _http_settings.auth_mode == "hybrid"
            else None
        )
        _app = build_http_app(
            mcp,
            transport,
            settings=_http_settings,
            token_validator=_mcp_token_validator,
            lifecycle=_runtime_lifecycle,
            static_token_validator=_mcp_static_token_validator,
        )
        if transport == "streamable-http":
            logger.info("MCP 单连接器 /mcp：16 个工具统一对外暴露")
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        logger.info(
            "MCP request body limit: %s",
            "disabled"
            if _http_settings.max_request_bytes == 0
            else f"{_http_settings.max_request_bytes} bytes",
        )

        _mcp_auth_required = _http_settings.auth_required
        if _mcp_auth_required and _http_settings.auth_mode == "token":
            logger.info(
                "MCP 静态 Token 鉴权已启用（OAuth 端点已关闭）/ "
                "MCP static-token auth enabled (OAuth endpoints disabled)"
            )
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 静态 Token 等同万能密钥：拿到它的人能读写你的全部记忆。\n"
                "    该模式与 OAuth 互斥，本进程不再提供 OAuth 授权流程；请勿把本服务\n"
                "    直接暴露到公网，仅在可信内网或自带鉴权的隧道场景使用，并妥善保管、\n"
                "    定期轮换该 Token。\n"
                + "=" * 60
            )
        elif _mcp_auth_required and _http_settings.auth_mode == "hybrid":
            logger.info("MCP OAuth + 静态 Token 共存鉴权已启用")
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  共存模式保留 OAuth，同时接受预置静态 Token；静态 Token 等同万能密钥。\n"
                "    请仅向受信任客户端分发并定期轮换，不要提交到仓库或截图分享。\n"
                + "=" * 60
            )
        elif _mcp_auth_required:
            logger.info("MCP OAuth middleware enabled / MCP OAuth 中间件已启用")
        else:
            # 安全加固 #7：关掉鉴权 = /mcp 全裸奔，任何能连到端口的人都能读写全部记忆。
            # 从 info 升级为显著 WARNING，避免用户无意识地把大脑暴露到公网。
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 认证已关闭 (mcp_require_auth: false)：/mcp 无需任何令牌即可直连，\n"
                "    15 个记忆工具全部对外开放——任何能访问本端口的人都能读写你的全部记忆。\n"
                f"    本服务进程监听 {_BIND_HOST}，若端口暴露到局域网/公网，请务必用反代鉴权、防火墙\n"
                "    或仅绑定 127.0.0.1 保护；免鉴权只建议用于已确认的本机回环连接。\n"
                + "=" * 60
            )
        # 端口口径澄清（用户反馈：Docker 与裸机端口容易混淆）。容器内固定监听 8000，
        # 对外端口由 host 映射（如 18001:8000）决定，改 host_port 不影响容器内监听；
        # 裸机则直接监听本端口（默认 18001）。
        if _wsh.in_docker():
            logger.info(
                f"Listening on :{OMBRE_PORT} INSIDE the container. "
                f"外部访问端口由 host 映射决定（compose 里的 18001:{OMBRE_PORT}），"
                f"改前端 host_port 不影响容器内监听。"
            )
        else:
            logger.info(f"Listening on :{OMBRE_PORT} (bare-metal / 裸机默认 18001)")
        # 明确打印「客户端该怎么连」——给 Operit / 安卓 / 自建前端等非技术用户排障用。
        # 一眼能看清 endpoint 路径、鉴权开关；本机桥接务必用 127.0.0.1（见上方保活注释）。
        _endpoint_path = "/mcp"
        logger.info(
            "MCP endpoint ready | transport=%s | 本机连接 URL: http://127.0.0.1:%s%s "
            "（远程走你的域名/隧道，末尾同样是 %s）| 鉴权: %s",
            transport,
            OMBRE_PORT,
            _endpoint_path,
            _endpoint_path,
            (
                "开启(需静态 Token)" if _http_settings.auth_mode == "token"
                else (
                    "开启(OAuth 或静态 Token)"
                    if _http_settings.auth_mode == "hybrid"
                    else "开启(需 OAuth Bearer)"
                )
            ) if _mcp_auth_required
            else "关闭(免 token 直连，仅限本机回环/显式高风险豁免)",
        )
        # Forwarded headers are validated inside the application against
        # OMBRE_TRUSTED_PROXY_CIDRS.  Uvicorn's default proxy middleware rewrites
        # scope["client"] before our guards run, which discards the immediate
        # proxy address and makes that trust decision impossible.
        uvicorn.run(
            _app,
            host=_BIND_HOST,
            port=OMBRE_PORT,
            proxy_headers=False,
        )
    elif transport == "stdio":
        # stdio：16 个工具已直接注册在唯一 mcp 实例上；启动成功边界由
        # FastMCP public lifespan 触发。向量队列必须与 HTTP 一样纳入生命周期，
        # 否则正文落盘后会退回同步索引，让慢 provider 拖住工具回包。
        _stdio_runtime_lifecycle = RuntimeLifecycle(
            logger=logger,
            embedding_outbox=embedding_outbox,
            boot_marker_path=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".boot_fails",
            ),
        )
        mcp.run(transport=transport)
    else:
        # 2026-08-09 起 legacy SSE transport 已下线。这里必须显式拒绝、不能落到
        # mcp.run(transport=transport) ——FastMCP 自带的 "sse" 字面量仍然合法，会
        # 绕过 build_http_app 里的鉴权/CORS/CSRF/限流中间件，直接起一个不受 Ombre
        # Brain 安全闸门保护的裸 SSE 服务，等于悄悄开一个没有认证的记忆读写口子。
        logger.error(
            f"不支持的 transport：{transport!r}。合法值仅 streamable-http、stdio；"
            "legacy SSE 传输已下线，请改用 streamable-http 并更新 config.yaml / "
            "OMBRE_TRANSPORT。"
        )
        raise SystemExit(1)
