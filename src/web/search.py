"""
========================================
web/search.py — 检索 / 重复 / 概念网络 / breath 调试
========================================

- /api/search：关键词+向量检索
- /api/duplicates：重复候选 pair（记忆健康面板）
- /api/network：概念网络图（wikilink + tag 共现）
- /api/breath、/api/breath-debug：breath 浮现结果 / 四维评分分解

对外暴露：register(mcp)。
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

try:
    from utils import strip_wikilinks, extract_wikilinks  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import strip_wikilinks, extract_wikilinks  # type: ignore


def register(mcp) -> None:

    @mcp.custom_route("/api/search", methods=["GET"])
    async def api_search(request: Request) -> Response:
        """Search buckets by query."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        query = request.query_params.get("q", "")
        if not query:
            return JSONResponse({"error": "missing q parameter"}, status_code=400)
        try:
            matches = await sh.bucket_mgr.search(query, limit=10)
            result = []
            for b in matches:
                meta = b.get("metadata", {})
                result.append({
                    "id": b["id"],
                    "name": meta.get("name", b["id"]),
                    "score": b.get("score", 0),
                    "domain": meta.get("domain", []),
                    "valence": meta.get("valence", 0.5),
                    "arousal": meta.get("arousal", 0.3),
                    "content_preview": strip_wikilinks(b.get("content", ""))[:200],
                })
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/duplicates", methods=["GET"])
    async def api_duplicates(request: Request) -> Response:
        """List bucket pairs flagged as duplicate candidates (sim > 0.95).

        iter 1.6 §4：每次 hold/grow 写完后 _check_duplicate_for 在两边写 dup_candidate +
        dup_score。本接口把所有这种标记的桶聚合成 pair，前端「记忆健康」面板可据此让
        她/他挨个确认是否合并。返回去重后的 pair 列表。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            all_b = await sh.bucket_mgr.list_all(include_archive=False)
            seen: set[frozenset] = set()
            pairs: list[dict] = []
            index = {b["id"]: b for b in all_b}
            for b in all_b:
                meta = b.get("metadata", {}) or {}
                other_id = meta.get("dup_candidate")
                if not other_id or other_id not in index:
                    continue
                key = frozenset((b["id"], other_id))
                if key in seen:
                    continue
                seen.add(key)
                other = index[other_id]
                pairs.append({
                    "a": {"id": b["id"], "name": meta.get("name", b["id"])},
                    "b": {"id": other_id, "name": other["metadata"].get("name", other_id)},
                    "score": meta.get("dup_score") or other["metadata"].get("dup_score"),
                })
            pairs.sort(key=lambda p: p.get("score") or 0, reverse=True)
            return JSONResponse({"pairs": pairs, "total": len(pairs)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/network", methods=["GET"])
    async def api_network(request: Request) -> Response:
        """Concept graph for visualization.

        iter 2.0+ §network rewrite: nodes are CONCEPT TOKENS that the user types
        inside their notes — `[[wikilinks]]` and frontmatter `tags`. Bucket
        filenames are NOT nodes. Two tokens get an edge whenever they co-occur
        in the same bucket. Edge weight = number of buckets containing both.

        iter 2.0+：节点 = 笔记里的双链词与 tag，不是文件名。两个词在同一个桶里出现就连一条边，
        边权重 = 共同出现的桶数。文件名只在前端搜索/详情里出现。

        Modes:
          - default `concept`：concept token graph (wikilinks + tags)
          - `embedding`：保留旧的桶级语义相似度网络（备用）
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        mode = (request.query_params.get("mode") or "concept").strip().lower()
        # 兼容旧入口 mode=wikilinks → 等价 concept
        if mode == "wikilinks":
            mode = "concept"
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)

            if mode == "embedding":
                # 旧的桶→桶相似度图（保留）
                nodes = []
                for b in all_buckets:
                    meta = b.get("metadata", {})
                    bid = b["id"]
                    nodes.append({
                        "id": bid,
                        "name": meta.get("name", bid),
                        "kind": "bucket",
                        "type": meta.get("type", "dynamic"),
                        "score": sh.decay_engine.calculate_score(meta),
                        "resolved": meta.get("resolved", False),
                        "pinned": meta.get("pinned", False),
                        "anchor": bool(meta.get("anchor")),  # #10
                    })
                edges = []
                embeddings = {}
                if sh.embedding_engine and sh.embedding_engine.enabled:
                    for b in all_buckets:
                        emb = await sh.embedding_engine.get_embedding(b["id"])
                        if emb is not None:
                            embeddings[b["id"]] = emb
                ids = list(embeddings.keys())
                for i, id_a in enumerate(ids):
                    for id_b in ids[i + 1:]:
                        sim = sh.embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > 0.5:
                            edges.append({"source": id_a, "target": id_b, "weight": round(sim, 3), "kind": "similarity"})
                return JSONResponse({"nodes": nodes, "edges": edges, "mode": mode})

            # ---- concept mode ----
            # token_id → {"label": str, "kind": "wiki"|"tag"|"mixed", "freq": int, "buckets": [bucket_id...]}
            # token_id 用规范化后的 lower-case 文本作 key，避免 "Memory" 与 "memory" 拆成两个节点
            tokens: dict[str, dict] = {}
            # bucket_id → set(token_id)，给后面共现统计用
            bucket_tokens: dict[str, set] = {}

            def _norm(s: str) -> str:
                return (s or "").strip()

            for b in all_buckets:
                bid = b["id"]
                meta = b.get("metadata", {}) or {}
                content = b.get("content", "") or ""

                seen: set[str] = set()
                # 1) 笔记正文里的 [[wikilinks]]
                for ref in extract_wikilinks(content):
                    label = _norm(ref)
                    if not label:
                        continue
                    key = label.lower()
                    node = tokens.setdefault(key, {"label": label, "kind": "wiki", "freq": 0, "buckets": []})
                    if key not in seen:
                        node["freq"] += 1
                        node["buckets"].append(bid)
                        seen.add(key)
                    # wiki 优先；若曾被标记为 tag，升级为 mixed
                    if node["kind"] == "tag":
                        node["kind"] = "mixed"

                # 2) frontmatter 的 tags（list 或字符串都兼容）
                raw_tags = meta.get("tags") or []
                if isinstance(raw_tags, str):
                    raw_tags = [t.strip() for t in raw_tags.split(",")]
                for t in raw_tags:
                    label = _norm(str(t)).lstrip("#")
                    if not label:
                        continue
                    key = label.lower()
                    node = tokens.setdefault(key, {"label": label, "kind": "tag", "freq": 0, "buckets": []})
                    if key not in seen:
                        node["freq"] += 1
                        node["buckets"].append(bid)
                        seen.add(key)
                    if node["kind"] == "wiki":
                        node["kind"] = "mixed"

                if seen:
                    bucket_tokens[bid] = seen

            # 共现边：同一个桶里的 token 两两相连，权重 = 共同出现的桶数
            # 复杂度上限是 sum(k_i^2) 其中 k_i 是单桶 token 数；正常都很小
            co_count: dict[tuple[str, str], int] = {}
            for bid, toks in bucket_tokens.items():
                ts = sorted(toks)
                for i, a in enumerate(ts):
                    for b_ in ts[i + 1:]:
                        co_key: tuple[str, str] = (a, b_)
                        co_count[co_key] = co_count.get(co_key, 0) + 1

            # #10: 标记「出现在至少一个 anchor 桶里」的 concept token
            anchor_bucket_ids = {
                b["id"] for b in all_buckets
                if (b.get("metadata") or {}).get("anchor")
            }
            nodes = [
                {
                    "id": k, "label": v["label"], "kind": v["kind"],
                    "freq": v["freq"], "buckets": v["buckets"],
                    "anchor": bool(anchor_bucket_ids and any(bid in anchor_bucket_ids for bid in v["buckets"])),
                }
                for k, v in tokens.items()
            ]
            edges = [{"source": a, "target": b_, "weight": w, "kind": "cooccur"} for (a, b_), w in co_count.items()]

            return JSONResponse({"nodes": nodes, "edges": edges, "mode": mode})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    # =============================================================
    # /api/plans — iter 1.7 §G2  Plan kanban list (active / resolved / abandoned)
    # 计划列表（按状态分组），含 change_log 历史
    # =============================================================
    @mcp.custom_route("/api/plans", methods=["GET"])
    async def api_plans(request: Request) -> Response:
        """Return plan buckets grouped by status (looks like a kanban board).

        返回所有 type==plan 的桶，按 status 分三组：active / resolved / abandoned。
        每组内部按 updated_at 倒序（最近动过的在最上面）。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            # 三个空桶子，后面按 status 往里填
            # 类型标注 dict[str, list] 是 Python 3.9+ 语法，不要变运行 IDE 报错
            groups: dict[str, list] = {"active": [], "resolved": [], "abandoned": []}
            for b in all_buckets:
                meta = b.get("metadata", {})
                # 过滤：只要计划类，跳过其他类型的桶
                if meta.get("type") != "plan":
                    continue
                # status 不一定存在（老数据），默认 active；lower() 防御大小写
                st = (meta.get("status") or "active").lower()
                # 未知状态一律当 active 处理，避免 KeyError
                if st not in groups:
                    st = "active"
                groups[st].append({
                    "id": b["id"],
                    "name": meta.get("name") or "",
                    "content": b.get("content", ""),
                    "status": st,
                    "created_at": meta.get("created_at"),
                    "updated_at": meta.get("updated_at"),
                    "related_bucket": meta.get("related_bucket"),
                    "change_log": meta.get("change_log") or [],
                    "tags": meta.get("tags") or [],
                    "importance": meta.get("importance", 7),
                    # iter 1.8: 承诺重量与「为什么」
                    "weight": float(meta.get("weight", 0.5)) if meta.get("weight") is not None else 0.5,
                    "why_remembered": meta.get("why_remembered", ""),
                })
            # 每组按 updated_at 倒序。lambda 是匿名函数；key 函数指定「拿什么排序」
            # `or .. or ""` 堆叠保底：缺字段也不会报 NoneType < str 错
            # iter 1.8: active 列改为 (weight desc, updated_at desc) —— 重的计划在前。
            # 排序键是「越靠后越主」：先按 updated_at 倒序的列表上再按 weight 倒序会使 weight 作为主键，
            # 所以这里用组合 key。resolved/abandoned 只按 updated_at 倒序。
            groups["active"].sort(
                key=lambda p: (-float(p.get("weight") or 0.5), p.get("updated_at") or p.get("created_at") or ""),
                reverse=False,  # 已经用负号使 weight 高为小（排前）；updated_at 字符串低位为后，reverse=False 下新的在后。
            )
            # 反转一下让同 weight 下新的在前：用二次稳定排序。
            groups["active"].sort(
                key=lambda p: p.get("updated_at") or p.get("created_at") or "",
                reverse=True,
            )
            groups["active"].sort(
                key=lambda p: float(p.get("weight") or 0.5),
                reverse=True,
            )
            for k in ("resolved", "abandoned"):
                groups[k].sort(key=lambda p: p.get("updated_at") or p.get("created_at") or "", reverse=True)
            return JSONResponse({
                "active": groups["active"],
                "resolved": groups["resolved"],
                "abandoned": groups["abandoned"],
                # 生成器表达式：sum + len，不需要临时 list
                "total": sum(len(v) for v in groups.values()),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/plans/{bucket_id}/action", methods=["POST"])
    async def api_plans_action(request: Request) -> Response:
        """Frontend kanban actions: mark plan as resolved / abandoned / active, or edit content.

        前端看板操作：勾选/打叉/重新激活，或编辑正文。
        路由里的 {bucket_id} 会被 starlette 解析进 request.path_params。
        Body 示例：{"action": "resolve", "content": "..."} —— content 仅 edit 需要。

        返回码约定：
          400 = 请求参数错（缺字段/超大小/不是 plan）
          404 = 指定桃子不存在
          500 = 底层 update 失败或未知异常
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            bucket_id = request.path_params.get("bucket_id", "").strip()
            if not bucket_id:
                return JSONResponse({"error": "missing bucket_id"}, status_code=400)
            # await request.json() 会把 body 当作 JSON 解析，类型改错会报 ValueError
            body = await request.json()
            action = (body.get("action") or "").strip().lower()
            bucket = await sh.bucket_mgr.get(bucket_id)
            if not bucket:
                return JSONResponse({"error": f"plan not found: {bucket_id}"}, status_code=404)
            # 双重防御：这个端点只能动 plan 桃子，别的类型不允许
            if bucket.get("metadata", {}).get("type") != "plan":
                return JSONResponse({"error": "bucket is not a plan"}, status_code=400)

            old_meta = bucket.get("metadata", {})
            # 复制一份历史记录（避免 append 后意外修改原 bucket dict）
            history = list(old_meta.get("change_log") or [])
            from tools._common import append_plan_change_log
            updates: dict[str, object] = {}

            if action in ("resolve", "abandon", "reopen"):
                # action 名 → 目标 status 名 的映射表，比三串 if/elif 清爽
                new_status = {"resolve": "resolved", "abandon": "abandoned", "reopen": "active"}[action]
                old_status = old_meta.get("status", "active")
                # 同状态 noop：不记入历史，下面 updates 为空会走 noop 分支
                if new_status != old_status:
                    updates["status"] = new_status
                    history = append_plan_change_log(
                        history, "status",
                        **{"from": old_status, "to": new_status},
                    )
            elif action == "edit":
                new_content = body.get("content", "")
                # 双重检查：类型必须是字符串，且 strip 后非空
                if not isinstance(new_content, str) or not new_content.strip():
                    return JSONResponse({"error": "content required for edit"}, status_code=400)
                size_err = _check_content_size(new_content)
                if size_err:
                    return JSONResponse({"error": size_err}, status_code=400)
                updates["content"] = new_content.strip()
                history = append_plan_change_log(history, "edit")
            else:
                return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)

            # status 没变 且 不是 edit，成 noop。返回 200 + ok=true，不报错
            if not updates:
                return JSONResponse({"ok": True, "noop": True})
            updates["change_log"] = history
            ok = await sh.bucket_mgr.update(bucket_id, **updates)
            if not ok:
                return JSONResponse({"error": "update failed"}, status_code=500)
            # 改了正文 → embedding 也要重新生成（否则检索会拿老向量不准）
            # 这里故意吞异常：embedding 完全可能因为网络/配额失败，不能堆出去让前端以为保存干脆了
            if "content" in updates and isinstance(updates["content"], str):
                try:
                    await sh.embedding_engine.generate_and_store(bucket_id, updates["content"])
                except Exception:
                    pass
            # --- plan 看板把 plan 显式标 resolved → 联动 related_bucket / resolved_by ---
            # rule.md §1：与 trace_core 同一逻辑（人工/Claude 显式路径）。
            cascaded: list[str] = []
            if updates.get("status") == "resolved":
                from tools._common import cascade_plan_resolved_to_buckets
                merged_meta = {**old_meta, **{k: v for k, v in updates.items() if k != "change_log"}}
                try:
                    cascaded = await cascade_plan_resolved_to_buckets(merged_meta, bucket_id)
                except Exception as e:
                    logger.warning(f"plans/action cascade outer error: {e}")
            # 返回体不包含 change_log（它很长，前端会重拉 /api/plans 刷新）
            return JSONResponse({
                "ok": True,
                "id": bucket_id,
                "updates": {k: v for k, v in updates.items() if k != "change_log"},
                "cascaded_resolved": cascaded,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/breath", methods=["GET"])
    async def api_breath(request: Request) -> Response:
        """Lightweight breath surface: returns top-N buckets by decay score."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            n = min(int(request.query_params.get("n", "10")), 50)
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            results = []
            for bucket in all_buckets:
                meta = bucket.get("metadata", {})
                score = sh.decay_engine.calculate_score(meta)
                if meta.get("resolved"):
                    score *= 0.3
                results.append({
                    "id": bucket["id"],
                    "name": meta.get("name", bucket["id"]),
                    "score": round(score, 4),
                    "domain": meta.get("domain", []),
                    "type": meta.get("type", "dynamic"),
                })
            results.sort(key=lambda x: x["score"], reverse=True)
            return JSONResponse({"buckets": results[:n]})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/breath-debug", methods=["GET"])
    async def api_breath_debug(request: Request) -> Response:
        """Debug endpoint: simulate breath scoring and return per-bucket breakdown."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        query = request.query_params.get("q", "")
        _qv_raw = request.query_params.get("valence")
        _qa_raw = request.query_params.get("arousal")
        q_valence: float | None = float(_qv_raw) if _qv_raw else None
        q_arousal: float | None = float(_qa_raw) if _qa_raw else None

        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            results = []
            w = {
                "topic": sh.bucket_mgr.w_topic,
                "emotion": sh.bucket_mgr.w_emotion,
                "time": sh.bucket_mgr.w_time,
                "importance": sh.bucket_mgr.w_importance,
            }
            w_sum = sum(w.values())

            for bucket in all_buckets:
                meta = bucket.get("metadata", {})
                bid = bucket["id"]
                try:
                    topic = sh.bucket_mgr._calc_topic_score(query, bucket) if query else 0.0
                    emotion = sh.bucket_mgr._calc_emotion_score(q_valence if q_valence is not None else 0.5, q_arousal if q_arousal is not None else 0.5, meta)
                    time_s = sh.bucket_mgr._calc_time_score(meta)
                    imp = max(1, min(10, int(meta.get("importance") or 5))) / 10.0

                    raw_total = (
                        topic * w["topic"]
                        + emotion * w["emotion"]
                        + time_s * w["time"]
                        + imp * w["importance"]
                    )
                    normalized = (raw_total / w_sum) * 100 if w_sum > 0 else 0
                    resolved = meta.get("resolved", False)
                    if resolved:
                        normalized *= 0.3

                    results.append({
                        "id": bid,
                        "name": meta.get("name", bid),
                        "domain": meta.get("domain", []),
                        "type": meta.get("type", "dynamic"),
                        "resolved": resolved,
                        "pinned": meta.get("pinned", False),
                        "scores": {
                            "topic": round(topic, 4),
                            "emotion": round(emotion, 4),
                            "time": round(time_s, 4),
                            "importance": round(imp, 4),
                        },
                        "weights": w,
                        "raw_total": round(raw_total, 4),
                        "normalized": round(normalized, 2),
                        "passed_threshold": normalized >= sh.bucket_mgr.fuzzy_threshold,
                    })
                except Exception as _score_exc:
                    logger.error(
                        f"Scoring failed for bucket {bid!r}: {type(_score_exc).__name__}: {_score_exc}",
                        exc_info=True,
                    )
                    continue

            results.sort(key=lambda x: x["normalized"], reverse=True)
            passed = [r for r in results if r["passed_threshold"]]
            return JSONResponse({
                "query": query,
                "valence": q_valence,
                "arousal": q_arousal,
                "weights": w,
                "threshold": sh.bucket_mgr.fuzzy_threshold,
                "total_candidates": len(results),
                "passed_count": len(passed),
                "results": results[:50],  # top 50 for debug
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
