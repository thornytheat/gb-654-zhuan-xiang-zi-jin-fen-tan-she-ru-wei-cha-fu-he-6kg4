"""复核合并纯函数：字段级三方合并、争议判定、权重测算。

合并规则（相对活动基准）：
- 无意见更正的字段保留基准值，来源 baseline；
- 单一更正值（一人或多人同值）直接采信，来源 opinion:<署名>；
- 同字段多个更正值形成争议，保留基准值，项目暂停参与权重；
- 有效裁决（seen_seq 不落后于该字段最新意见序号）优先采信；
- 裁决后出现新意见的，旧裁决读取时自动失效，争议重开。
资格判定复用 M2 evaluate，权重归一复用 M3 normalize，均不修改其规则。
"""
import json
from decimal import Decimal, InvalidOperation
from app.errors import DomainError
from app.reasons import reason
from app.modules.m2_eligibility.core import evaluate
from app.modules.m3_weights.core import normalize

_MISSING = object()


def canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def type_ok(base_value, new_value) -> bool:
    if isinstance(base_value, bool):
        return isinstance(new_value, bool)
    if isinstance(base_value, (int, float)):
        return isinstance(new_value, (int, float)) and not isinstance(new_value, bool)
    return isinstance(new_value, type(base_value))


def check_weight(value) -> None:
    try:
        weight = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DomainError("INVALID_VALUE", "权重须为数值: " + str(value))
    if weight < 0:
        raise DomainError("NEGATIVE_WEIGHT", reason("NEGATIVE_WEIGHT"))


def _adjudication_state(opinions, adjudications):
    """按 (项目, 字段) 汇总意见最大序号、有效裁决与已失效裁决历史。"""
    max_seq = {}
    for op in opinions:
        key = (op["project_id"], op["field"])
        max_seq[key] = max(max_seq.get(key, 0), op["seq"])
    history = {}
    for adj in adjudications:
        history.setdefault((adj["project_id"], adj["field"]), []).append(adj)
    valid, invalidated = {}, {}
    for key, group in history.items():
        ordered = sorted(group, key=lambda a: a["id"])
        if ordered[-1]["seen_seq"] >= max_seq.get(key, 0):
            valid[key] = ordered[-1]
            invalidated[key] = ordered[:-1]
        else:
            invalidated[key] = ordered
    return valid, invalidated


def merge_projects(base_projects, opinions, adjudications):
    """以基准为底逐字段合并，返回 (merged, disputes)。输出与意见收件顺序无关。"""
    by_field = {}
    for op in opinions:
        by_field.setdefault((op["project_id"], op["field"]), []).append(op)
    valid_adj, invalidated_adj = _adjudication_state(opinions, adjudications)
    merged, disputes = [], []
    for base in base_projects:
        pid = base["id"]
        extra = {field for (owner, field) in by_field if owner == pid}
        values, sources, open_fields = {}, {}, []
        for field in sorted(set(base) | extra):
            base_val = base.get(field, _MISSING)
            base_canon = canon(base_val) if base_val is not _MISSING else None
            changed = {}
            for op in by_field.get((pid, field), []):
                if canon(op["value"]) != base_canon:
                    entry = changed.setdefault(canon(op["value"]), {"value": op["value"], "reviewers": set()})
                    entry["reviewers"].add(op["reviewer"])
            key = (pid, field)
            adj = valid_adj.get(key)
            if len(changed) >= 2:
                positions = [{"value": e["value"], "reviewers": sorted(e["reviewers"])}
                             for e in sorted(changed.values(), key=lambda x: canon(x["value"]))]
                disputes.append({
                    "project_id": pid, "field": field,
                    "base_value": None if base_val is _MISSING else base_val,
                    "positions": positions,
                    "status": "resolved" if adj else "open",
                    "adjudication": ({"value": adj["value"], "decided_by": adj["decided_by"],
                                      "seen_seq": adj["seen_seq"]} if adj else None),
                    "invalidated": [{"value": a["value"], "decided_by": a["decided_by"],
                                     "seen_seq": a["seen_seq"]} for a in invalidated_adj.get(key, [])],
                })
            if adj is not None:
                values[field], sources[field] = adj["value"], "adjudication:" + adj["decided_by"]
            elif not changed:
                if base_val is not _MISSING:
                    values[field], sources[field] = base_val, "baseline"
            elif len(changed) == 1:
                entry = next(iter(changed.values()))
                values[field] = entry["value"]
                sources[field] = "opinion:" + "+".join(sorted(entry["reviewers"]))
            else:
                values[field] = None if base_val is _MISSING else base_val
                sources[field] = "disputed"
                open_fields.append(field)
        decision = evaluate({**values, "id": pid})
        merged.append({"id": pid, "values": values, "sources": sources,
                       "eligible": decision["eligible"], "reason": decision["reason"],
                       "disputed_fields": sorted(open_fields)})
    return merged, disputes


def compute_weights(merged_projects):
    """争议项目暂停、资格不通过项目排除，其余项目按有效权重归一。"""
    participants, paused, excluded = [], [], []
    for rec in merged_projects:
        if rec["disputed_fields"]:
            paused.append({"id": rec["id"], "disputed_fields": rec["disputed_fields"]})
        elif not rec["eligible"]:
            excluded.append({"id": rec["id"], "reason": rec["reason"]})
        else:
            participants.append({"id": rec["id"],
                                 "weight": rec["values"].get("weight", "1"),
                                 "weight_source": rec["sources"].get("weight", "default"),
                                 "eligibility_reason": rec["reason"]})
    shares = {}
    if participants:
        try:
            normalized = normalize([{"id": p["id"], "weight": p["weight"]} for p in participants])
        except ValueError as exc:
            raise DomainError(str(exc), reason(str(exc)))
        shares = {x["id"]: str(x["share"]) for x in normalized}
    for p in participants:
        p["share"] = shares.get(p["id"], "0")
    share_sum = sum((Decimal(s) for s in shares.values()), Decimal(0))
    return {"participants": participants, "paused": paused, "excluded": excluded,
            "share_sum": str(share_sum.normalize()) if shares else "0",
            "reason": "OK" if participants else "ALL_PAUSED"}


def run_merge(snapshot):
    """由持久化快照计算一次合并结果（纯函数，失败时由调用方保留旧版）。"""
    active = next(b for b in snapshot["baselines"] if b["version"] == snapshot["active_version"])
    merged, disputes = merge_projects(active["projects"], snapshot["opinions"], snapshot["adjudications"])
    weights = compute_weights(merged)
    return {"base_version": active["version"], "merged": merged,
            "disputes": disputes, "weights": weights}
