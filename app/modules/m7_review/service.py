"""复核合并服务层：校验、幂等收件、裁决绑定、原子提交与回读组装。"""
from datetime import datetime, timezone
from app.errors import DomainError
from app.manifest import canonical_digest
from app.reasons import reason
from . import core


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require(payload: dict, *fields: str) -> None:
    missing = [f for f in fields if payload.get(f) is None]
    if missing:
        raise DomainError("EMPTY_VALUE", "缺少必要值: " + ",".join(missing))


def _state_digest(snapshot: dict) -> str:
    return canonical_digest({
        "base": snapshot["active_version"],
        "opinions": [{k: o[k] for k in ("seq", "reviewer", "base_version", "project_id", "field", "value")}
                     for o in snapshot["opinions"]],
        "adjudications": [{k: a[k] for k in ("id", "project_id", "field", "value", "decided_by", "seen_seq")}
                          for a in snapshot["adjudications"]],
    })


def submit_baseline(store, payload: dict) -> dict:
    _require(payload, "version", "projects")
    projects = payload["projects"]
    if not isinstance(projects, list) or not projects \
            or not all(isinstance(p, dict) and p.get("id") for p in projects):
        raise DomainError("INVALID_VALUE", "基准项目须为含 id 的对象列表")
    for p in projects:
        if "weight" in p:
            core.check_weight(p["weight"])
    return store.register_baseline(str(payload["version"]), projects, _now())


def submit_opinion(store, payload: dict) -> dict:
    _require(payload, "token", "reviewer", "base_version", "project_id", "field", "value")
    op = {"token": str(payload["token"]), "reviewer": str(payload["reviewer"]),
          "base_version": str(payload["base_version"]), "project_id": str(payload["project_id"]),
          "field": str(payload["field"]), "value": payload["value"],
          "note": str(payload.get("note") or ""), "received_at": _now()}
    existing = store.find_opinion(op["token"])
    if existing is not None:  # 重试：内容一致则返回原收件，不重复入库
        same = all(existing[k] == op[k] for k in ("reviewer", "base_version", "project_id", "field", "value", "note"))
        if not same:
            raise DomainError("DUPLICATE_CONFLICT", "同一令牌提交了不同内容: " + op["token"])
        return {"status": "RECEIVED", "seq": existing["seq"], "duplicate": True,
                "received_at": existing["received_at"]}
    snap = store.snapshot()
    if op["base_version"] not in {b["version"] for b in snap["baselines"]}:
        raise DomainError("UNKNOWN_BASELINE", reason("UNKNOWN_BASELINE") + ": " + op["base_version"])
    active = next(b for b in snap["baselines"] if b["version"] == snap["active_version"])
    project = next((p for p in active["projects"] if p["id"] == op["project_id"]), None)
    if project is None:
        raise DomainError("INVALID_VALUE", "未知项目: " + op["project_id"])
    allowed = (set().union(*(set(p) for p in active["projects"])) - {"id"}) | {"weight"}
    if op["field"] not in allowed:
        raise DomainError("INVALID_VALUE", "未知字段: " + op["field"])
    if op["field"] == "weight":
        core.check_weight(op["value"])
    elif op["field"] in project and not core.type_ok(project[op["field"]], op["value"]):
        raise DomainError("INVALID_VALUE", "值类型与基准不符: " + op["field"])
    receipt = store.add_opinion(op)
    return {"status": "RECEIVED", **receipt}


def submit_adjudication(store, payload: dict) -> dict:
    _require(payload, "project_id", "field", "value", "decided_by", "seen_seq")
    snap = store.snapshot()
    related = [o for o in snap["opinions"]
               if o["project_id"] == payload["project_id"] and o["field"] == payload["field"]]
    if not related:
        raise DomainError("NO_OPINIONS", reason("NO_OPINIONS"))
    max_seq = max(o["seq"] for o in related)
    if int(payload["seen_seq"]) != max_seq:  # 裁决必须绑定当前已见意见版本
        raise DomainError("STALE_VERSION", reason("STALE_VERSION"))
    receipt = store.add_adjudication({
        "project_id": str(payload["project_id"]), "field": str(payload["field"]),
        "value": payload["value"], "decided_by": str(payload["decided_by"]),
        "seen_seq": max_seq, "decided_at": _now()})
    return {"status": "DECIDED", "seen_seq": max_seq, **receipt}


def commit_merge(store) -> dict:
    snap = store.snapshot()
    if not snap["active_version"]:
        raise DomainError("EMPTY_VALUE", "尚未登记基准")
    result = core.run_merge(snap)  # 抛错则不写库，旧版本保留
    digest = _state_digest(snap)
    committed = store.commit(digest, snap["active_version"], result, _now())
    weights = result["weights"]
    return {"status": "READY", "version": committed["version"], "reused": committed["reused"],
            "digest": digest, "base_version": snap["active_version"],
            "open_disputes": sum(1 for d in result["disputes"] if d["status"] == "open"),
            "participants": len(weights["participants"]), "paused": len(weights["paused"]),
            "excluded": len(weights["excluded"]), "share_sum": weights["share_sum"],
            "reason": weights["reason"]}


def status(store) -> dict:
    """回读：原意见（含其所见基准原值）、争议、采信结果与权重来源。"""
    snap = store.snapshot()
    current = store.current()
    live, live_error = None, None
    if snap["active_version"]:
        try:
            live = core.run_merge(snap)
        except DomainError as exc:
            live_error = exc.code
    digest = _state_digest(snap) if snap["active_version"] else None
    base_map = {b["version"]: b for b in snap["baselines"]}
    opinions = []
    for o in snap["opinions"]:
        base = base_map.get(o["base_version"])
        project = next((p for p in base["projects"] if p["id"] == o["project_id"]), None) if base else None
        opinions.append({**o, "base_value": (project or {}).get(o["field"])})
    max_seq = {}
    for o in snap["opinions"]:
        key = (o["project_id"], o["field"])
        max_seq[key] = max(max_seq.get(key, 0), o["seq"])
    latest = {}
    for a in snap["adjudications"]:
        latest[(a["project_id"], a["field"])] = a["id"]
    adjudications = [{**a, "valid": a["id"] == latest[(a["project_id"], a["field"])]
                      and a["seen_seq"] >= max_seq.get((a["project_id"], a["field"]), 0)}
                     for a in snap["adjudications"]]
    result = current["result"] if current else {"merged": [], "disputes": [], "weights": None}
    weights = result["weights"] or {"participants": [], "paused": [], "excluded": [], "share_sum": "0", "reason": "PENDING"}
    disputes = live["disputes"] if live else result["disputes"]
    open_disputes = sum(1 for d in disputes if d["status"] == "open")
    return {
        "status": "READY",
        "version": current["version"] if current else 0,
        "digest": current["digest"] if current else None,
        "dirty": digest is not None and (current is None or current["digest"] != digest),
        "base_version": snap["active_version"],
        "baselines": [{"version": b["version"], "active": b["version"] == snap["active_version"],
                       "projects": b["projects"], "registered_at": b["registered_at"]}
                      for b in snap["baselines"]],
        "opinions": opinions,
        "adjudications": adjudications,
        "disputes": disputes,
        "merged": result["merged"],
        "weights": weights,
        "pending_error": live_error,
        "summary": {"version": current["version"] if current else 0,
                    "base_version": snap["active_version"] or "-",
                    "opinions": len(opinions), "open_disputes": open_disputes,
                    "participants": len(weights["participants"]), "paused": len(weights["paused"]),
                    "dirty": digest is not None and (current is None or current["digest"] != digest)},
    }
