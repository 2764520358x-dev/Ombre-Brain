"""Private, read-only diary browser for the authenticated dashboard."""

import os
import re
import tempfile
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_ENTRY_BYTES = 2 * 1024 * 1024
_MAX_SYNC_BYTES = 8 * 1024 * 1024
_MAX_SYNC_ENTRIES = 1000


def _diary_dir() -> Path:
    configured = os.environ.get("CYBERBOSS_DIARY_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    # Render's attached disk is the only filesystem area that survives a
    # deploy.  Keep diary files beside the existing Ombre data instead of in
    # the ephemeral service home directory.  The live service currently mounts
    # its disk at /data; deployments with a configured buckets_dir follow that
    # path automatically.
    if os.environ.get("RENDER", "").strip().lower() == "true":
        buckets_dir = str(sh.config.get("buckets_dir", "") or "").strip()
        return Path(buckets_dir or "/data") / "diary"
    return Path.home() / ".cyberboss" / "diary"


def register(mcp) -> None:
    @mcp.custom_route("/api/diary", methods=["GET"])
    async def api_diary(request: Request) -> Response:
        from starlette.responses import JSONResponse

        err = sh._require_auth(request)
        if err:
            return err

        root = _diary_dir()
        date = request.query_params.get("date", "").strip()
        try:
            if date:
                if not _DATE_RE.fullmatch(date):
                    return JSONResponse({"error": "invalid date"}, status_code=400)
                path = root / f"{date}.md"
                if not path.is_file():
                    return JSONResponse({"error": "diary entry not found"}, status_code=404)
                if path.stat().st_size > _MAX_ENTRY_BYTES:
                    return JSONResponse({"error": "diary entry is too large"}, status_code=413)
                return JSONResponse(
                    {"date": date, "content": path.read_text(encoding="utf-8")},
                    headers={"Cache-Control": "no-store"},
                )

            dates = []
            if root.is_dir():
                dates = sorted(
                    (p.stem for p in root.glob("*.md") if _DATE_RE.fullmatch(p.stem)),
                    reverse=True,
                )
            return JSONResponse(
                {"dates": dates, "total": len(dates)},
                headers={"Cache-Control": "no-store"},
            )
        except OSError:
            return JSONResponse({"error": "unable to read diary"}, status_code=500)

    @mcp.custom_route("/api/diary/sync", methods=["POST"])
    async def api_diary_sync(request: Request) -> Response:
        """Synchronize diary Markdown over TLS using dashboard or MCP auth.

        This endpoint only writes date-named files inside the dedicated diary
        directory.  It never deletes entries and never touches memory buckets.
        """
        from starlette.responses import JSONResponse
        from .oauth import _is_valid_static_mcp_token

        auth_error = sh._require_auth(request)
        if auth_error:
            authorization = request.headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not _is_valid_static_mcp_token(token.strip()):
                return auth_error

        try:
            raw = await request.body()
            if len(raw) > _MAX_SYNC_BYTES:
                return JSONResponse({"error": "sync payload is too large"}, status_code=413)
            import json
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        entries = body.get("entries") if isinstance(body, dict) else None
        if not isinstance(entries, list) or len(entries) > _MAX_SYNC_ENTRIES:
            return JSONResponse({"error": "invalid entries"}, status_code=400)

        validated = []
        for item in entries:
            if not isinstance(item, dict):
                return JSONResponse({"error": "invalid diary entry"}, status_code=400)
            date = str(item.get("date", "")).strip()
            content = item.get("content")
            if not _DATE_RE.fullmatch(date) or not isinstance(content, str):
                return JSONResponse({"error": "invalid diary entry"}, status_code=400)
            if len(content.encode("utf-8")) > _MAX_ENTRY_BYTES:
                return JSONResponse({"error": f"diary entry is too large: {date}"}, status_code=413)
            validated.append((date, content))

        root = _diary_dir()
        written = 0
        try:
            root.mkdir(parents=True, exist_ok=True)
            for date, content in validated:
                target = root / f"{date}.md"
                fd, temp_name = tempfile.mkstemp(prefix=f".{date}.", suffix=".tmp", dir=root)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temp_name, 0o600)
                    os.replace(temp_name, target)
                    written += 1
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
        except OSError:
            return JSONResponse({"error": "unable to write diary"}, status_code=500)

        return JSONResponse(
            {"ok": True, "written": written},
            headers={"Cache-Control": "no-store"},
        )
