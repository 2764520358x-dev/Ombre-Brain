"""
json_backup.py — 全库 JSON 导出 & GitHub 推送

功能：
- 将所有桶（动态、固化、归档、情绪、计划、信件、自我认知）导出为单个 JSON 快照
- 按日期命名（<backup_prefix>/YYYY-MM-DD.json）推送到 GitHub 私有仓库
- 保留全部历史版本：每天一个新文件，旧文件不覆盖
- 支持定时自动备份（asyncio 后台循环）+ HTTP 手动触发

推送方式：GitHub Git Trees API（与 github_sync.py 同策略，单次 API 调用 = 一个 commit）

关于 git add 范围：
  本模块通过 GitHub API 精确指定推送路径（backup/<date>.json），
  不涉及 .github/workflows/ 等目录，不受 GitHub Actions GITHUB_TOKEN 默认权限限制。
  若你在 GitHub Actions 中用 git 命令提交，请始终用
      git add backup/
  而非 git add . ——后者会把 workflow 文件本身带入提交，导致 push 被拒绝。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

try:
    import frontmatter as _frontmatter
except ImportError:
    _frontmatter = None  # type: ignore

logger = logging.getLogger("ombre_brain.json_backup")

_API = "https://api.github.com"
_TIMEOUT = 60.0
_MAX_RETRIES = 4

# 备份时扫描的所有子目录（含 archive，不含 embeddings.db 等二进制文件）
_BACKUP_SUBDIRS = ["permanent", "dynamic", "archive", "feel", "plans", "letters", "i"]


class JsonBackupManager:
    """全库 JSON 导出 + 推送到 GitHub 私有仓库。"""

    def __init__(
        self,
        token: str,
        repo: str,
        branch: str = "main",
        backup_prefix: str = "backup",
    ) -> None:
        self.token = token
        self.repo = repo.strip()
        self.branch = branch.strip() or "main"
        self.backup_prefix = backup_prefix.strip().strip("/") or "backup"

        self._headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        self.last_backup: str | None = None
        self.last_status: str = "idle"
        self.last_error: str = ""
        self.last_size_kb: float = 0.0
        self.last_count: int = 0

    # --------------------------------------------------------
    # 公开接口
    # --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.token and self.repo),
            "repo": self.repo,
            "branch": self.branch,
            "backup_prefix": self.backup_prefix,
            "last_backup": self.last_backup,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_size_kb": self.last_size_kb,
            "last_count": self.last_count,
        }

    def export_all(self, buckets_dir: str, version: str = "") -> dict[str, Any]:
        """扫描 buckets_dir 下所有 .md 文件，返回 JSON 可序列化 dict。

        同步方法（耗时微秒级），在 asyncio 环境中可直接调用无需 run_in_executor。
        """
        if _frontmatter is None:
            raise RuntimeError("python-frontmatter 未安装，无法解析桶文件")

        buckets: list[dict] = []
        for subdir in _BACKUP_SUBDIRS:
            full_subdir = os.path.join(buckets_dir, subdir)
            if not os.path.isdir(full_subdir):
                continue
            for root, _, files in os.walk(full_subdir):
                for fn in sorted(files):
                    if not fn.endswith(".md"):
                        continue
                    fpath = os.path.join(root, fn)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            post = _frontmatter.load(f)
                        bucket: dict = dict(post.metadata)
                        bucket["content"] = post.content
                        bucket["_rel_path"] = os.path.relpath(fpath, buckets_dir).replace("\\", "/")
                        buckets.append(bucket)
                    except Exception as e:
                        logger.warning(f"[json_backup] skip {fpath}: {e}")

        now = _now_iso()
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return {
            "version": version or "unknown",
            "exported_at": now,
            "date": date_str,
            "total_count": len(buckets),
            "buckets": buckets,
        }

    async def run_backup(self, buckets_dir: str, version: str = "") -> dict[str, Any]:
        """完整备份周期：导出 JSON → 推送到 GitHub。返回结果 dict。"""
        try:
            data = self.export_all(buckets_dir, version)
            date_str = data["date"]
            json_str = json.dumps(data, ensure_ascii=False, indent=2, default=str)
            size_kb = len(json_str.encode("utf-8")) / 1024

            gh_path = f"{self.backup_prefix}/{date_str}.json"
            commit_msg = f"backup: {date_str} ({data['total_count']} buckets)"
            result = await self._push_file(gh_path, json_str, commit_msg)

            if result.get("ok"):
                self.last_backup = _now_iso()
                self.last_status = "ok"
                self.last_error = ""
                self.last_size_kb = round(size_kb, 1)
                self.last_count = data["total_count"]
                return {
                    "ok": True,
                    "date": date_str,
                    "total_count": data["total_count"],
                    "size_kb": round(size_kb, 1),
                    "commit_sha": result.get("commit_sha"),
                    "path": gh_path,
                }
            else:
                self.last_status = "error"
                self.last_error = result.get("error", "unknown")
                return result

        except Exception as e:
            self.last_status = "error"
            self.last_error = str(e)
            logger.error(f"[json_backup] run_backup failed: {e}")
            return {"ok": False, "error": str(e)}

    # --------------------------------------------------------
    # 内部实现
    # --------------------------------------------------------

    async def _push_file(self, path: str, content: str, commit_msg: str) -> dict[str, Any]:
        """通过 GitHub Git Trees API 推送单个文件（一次 API 调用 = 一个 commit）。

        git add 范围说明：这里只向 GitHub API 提交 path 参数所指定的单个文件路径，
        不会触碰 .github/workflows/ 或任何其他目录——完全避开 GITHUB_TOKEN 权限限制。
        """
        async with httpx.AsyncClient(headers=self._headers, timeout=_TIMEOUT) as c:
            # 1. 获取 branch HEAD commit SHA
            r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/ref/heads/{self.branch}")
            if r.status_code == 404:
                raise RuntimeError(
                    f"分支 {self.branch} 不存在，请先在 GitHub 上创建该分支（可以是一个空仓库的默认分支）"
                )
            r.raise_for_status()
            head_sha: str = r.json()["object"]["sha"]

            # 2. 获取 HEAD commit 对应的 tree SHA
            r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/commits/{head_sha}")
            r.raise_for_status()
            base_tree_sha: str = r.json()["tree"]["sha"]

            # 3. 创建新 tree（单文件内联 content，GitHub 自动生成 blob）
            r = await self._request(
                c, "POST", f"{_API}/repos/{self.repo}/git/trees",
                json={
                    "base_tree": base_tree_sha,
                    "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}],
                },
            )
            r.raise_for_status()
            new_tree_sha: str = r.json()["sha"]

            # 4. 创建 commit
            r = await self._request(
                c, "POST", f"{_API}/repos/{self.repo}/git/commits",
                json={"message": commit_msg, "tree": new_tree_sha, "parents": [head_sha]},
            )
            r.raise_for_status()
            commit_sha: str = r.json()["sha"]

            # 5. 更新 branch ref（force=False 保证不会误覆盖同名 commit）
            r = await self._request(
                c, "PATCH", f"{_API}/repos/{self.repo}/git/refs/heads/{self.branch}",
                json={"sha": commit_sha, "force": False},
            )
            r.raise_for_status()
            return {"ok": True, "commit_sha": commit_sha}

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json: dict | None = None,
    ) -> httpx.Response:
        """带指数退避重试，专治 GitHub 二级限流（403/429）。"""
        for attempt in range(_MAX_RETRIES + 1):
            resp = await client.request(method, url, json=json)
            if resp.status_code not in (403, 429):
                return resp
            body_l = resp.text.lower()
            is_rate = (
                "rate limit" in body_l
                or "retry-after" in {k.lower() for k in resp.headers}
                or resp.headers.get("x-ratelimit-remaining") == "0"
            )
            if not is_rate or attempt == _MAX_RETRIES:
                return resp
            retry_after = resp.headers.get("retry-after")
            wait = int(retry_after) if (retry_after and retry_after.isdigit()) else min(2 ** attempt, 30)
            logger.warning(f"[json_backup] rate limit, retry in {wait}s (attempt {attempt + 1})")
            await asyncio.sleep(wait)
        return resp


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
