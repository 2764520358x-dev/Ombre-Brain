"""
web/backup.py — 全库 JSON 备份路由

GET  /api/backup/status  — 查看备份状态与配置
POST /api/backup/config  — 保存备份配置（token/repo/branch/backup_prefix/interval）
POST /api/backup/run     — 手动立即触发一次备份

配置优先级：OMBRE_BACKUP_TOKEN 环境变量 > backup_export.token (config.yaml)
备份推送路径：<backup_prefix>/<YYYY-MM-DD>.json（每天一个新文件，历史永久保留）

对外暴露：register(mcp)
"""

import os

import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import _shared as sh

logger = sh.logger


def register(mcp) -> None:

    @mcp.custom_route("/api/backup/status", methods=["GET"])
    async def api_backup_status(request: Request) -> JSONResponse:
        err = sh._require_auth(request)
        if err:
            return err
        cfg = sh.config.get("backup_export", {}) or {}
        auto_hours = int(cfg.get("auto_interval_hours") or 24)
        if sh.backup_manager is None:
            return JSONResponse({
                "ok": True,
                "configured": False,
                "repo": cfg.get("repo", ""),
                "branch": cfg.get("branch", "main"),
                "backup_prefix": cfg.get("backup_prefix", "backup"),
                "token_set": bool(os.environ.get("OMBRE_BACKUP_TOKEN") or cfg.get("token")),
                "auto_interval_hours": auto_hours,
            })
        return JSONResponse({
            "ok": True,
            "configured": True,
            "auto_interval_hours": auto_hours,
            **sh.backup_manager.status(),
        })

    @mcp.custom_route("/api/backup/config", methods=["POST"])
    async def api_backup_config(request: Request) -> JSONResponse:
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效 JSON"}, status_code=400)

        try:
            from json_backup import JsonBackupManager  # type: ignore
        except ImportError:
            from ..json_backup import JsonBackupManager  # type: ignore

        try:
            from utils import config_file_path  # type: ignore
        except ImportError:
            from ..utils import config_file_path  # type: ignore

        token = str(body.get("token") or "").strip()
        repo = str(body.get("repo") or "").strip()
        branch = str(body.get("branch") or "main").strip() or "main"
        backup_prefix = str(body.get("backup_prefix") or "backup").strip() or "backup"
        auto_hours = int(body.get("auto_interval_hours") or 24)

        cfg = sh.config.setdefault("backup_export", {})
        if token:
            cfg["token"] = token
        cfg["repo"] = repo
        cfg["branch"] = branch
        cfg["backup_prefix"] = backup_prefix
        cfg["auto_interval_hours"] = auto_hours

        config_path = config_file_path()
        try:
            save_config: dict = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}
            sc = save_config.setdefault("backup_export", {})
            if token:
                sc["token"] = token
            sc["repo"] = repo
            sc["branch"] = branch
            sc["backup_prefix"] = backup_prefix
            sc["auto_interval_hours"] = auto_hours
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_config, f, allow_unicode=True, default_flow_style=False)
        except Exception as e:
            logger.warning(f"[backup] config.yaml 写入失败: {e}")

        effective_token = (
            token
            or sh.config.get("backup_export", {}).get("token")
            or os.environ.get("OMBRE_BACKUP_TOKEN", "")
        )
        if effective_token and repo:
            sh.backup_manager = JsonBackupManager(
                token=effective_token, repo=repo, branch=branch, backup_prefix=backup_prefix
            )
        else:
            sh.backup_manager = None

        if sh.restart_backup_task is not None:
            sh.restart_backup_task(auto_hours)

        return JSONResponse({"ok": True, "message": "备份配置已保存"})

    @mcp.custom_route("/api/backup/run", methods=["POST"])
    async def api_backup_run(request: Request) -> JSONResponse:
        err = sh._require_auth(request)
        if err:
            return err
        if sh.backup_manager is None:
            return JSONResponse(
                {"ok": False, "error": "尚未配置备份，请先 POST /api/backup/config"},
                status_code=400,
            )
        buckets_dir = sh.config.get("buckets_dir", "")
        if not buckets_dir:
            return JSONResponse({"ok": False, "error": "buckets_dir 未配置"}, status_code=500)
        result = await sh.backup_manager.run_backup(buckets_dir, sh.version or "")
        return JSONResponse(result)
