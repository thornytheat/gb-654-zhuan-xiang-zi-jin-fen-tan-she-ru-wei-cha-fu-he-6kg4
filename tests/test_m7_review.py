import pytest
from app.errors import DomainError
from app.modules.m7_review import core, service
from app.modules.m7_review.store import ReviewStore

BASE = {"version": "v1", "projects": [
    {"id": "A", "region": "rural", "score": 82, "blacklisted": False, "weight": "2"},
    {"id": "B", "region": "urban", "score": 95, "blacklisted": True, "weight": "1"},
    {"id": "C", "region": "rural", "score": 65, "blacklisted": False, "weight": "1"},
]}


def op(token, reviewer, pid, field, value, base="v1", note=""):
    return {"token": token, "reviewer": reviewer, "base_version": base,
            "project_id": pid, "field": field, "value": value, "note": note}


@pytest.fixture
def store(tmp_path):
    s = ReviewStore(str(tmp_path / "review.db"))
    service.submit_baseline(s, BASE)
    return s


def merged_of(store):
    return store.current()["result"]


# --- 合并语义：吸收无冲突更正，分歧留待裁决 ---

def test_absorb_non_conflicting(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    service.submit_opinion(store, op("t2", "乙", "A", "region", "urban"))
    service.submit_opinion(store, op("t3", "丙", "B", "weight", "3"))
    service.commit_merge(store)
    merged = {p["id"]: p for p in merged_of(store)["merged"]}
    assert merged["A"]["values"]["score"] == 88
    assert merged["A"]["sources"]["score"] == "opinion:甲"
    assert merged["A"]["values"]["region"] == "urban"
    assert merged["B"]["values"]["weight"] == "3"
    assert merged_of(store)["disputes"] == []


def test_same_value_agreement_not_dispute(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    service.submit_opinion(store, op("t2", "乙", "A", "score", 88))
    service.commit_merge(store)
    merged = merged_of(store)["merged"][0]
    assert merged["values"]["score"] == 88
    assert merged["sources"]["score"] == "opinion:乙+甲"  # 署名按码位排序，确定性输出
    assert merged_of(store)["disputes"] == []


def test_conflict_pauses_weight_others_continue(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    service.submit_opinion(store, op("t2", "乙", "A", "score", 90))
    service.commit_merge(store)
    result = merged_of(store)
    dispute = result["disputes"][0]
    assert dispute["status"] == "open" and dispute["field"] == "score"
    assert {tuple(p["reviewers"]) for p in dispute["positions"]} == {("甲",), ("乙",)}
    merged_a = result["merged"][0]
    assert merged_a["values"]["score"] == 82  # 争议字段保留基准值
    assert merged_a["sources"]["score"] == "disputed"
    weights = result["weights"]
    assert [p["id"] for p in weights["paused"]] == ["A"]  # 争议项目暂停
    assert [p["id"] for p in weights["excluded"]] == ["B"]  # 黑名单不通过
    assert [(p["id"], p["share"]) for p in weights["participants"]] == [("C", "1")]
    assert weights["share_sum"] == "1"


def test_adjudication_resolves_and_resumes(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    service.submit_opinion(store, op("t2", "乙", "A", "score", 90))
    decided = service.submit_adjudication(store, {
        "project_id": "A", "field": "score", "value": 89,
        "decided_by": "裁决员", "seen_seq": 2})
    assert decided["status"] == "DECIDED" and decided["seen_seq"] == 2
    service.commit_merge(store)
    result = merged_of(store)
    merged_a = result["merged"][0]
    assert merged_a["values"]["score"] == 89
    assert merged_a["sources"]["score"] == "adjudication:裁决员"
    assert result["disputes"][0]["status"] == "resolved"
    assert [p["id"] for p in result["weights"]["participants"]] == ["A", "C"]
    assert result["weights"]["paused"] == []


def test_concurrent_opinion_invalidates_adjudication(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    service.submit_opinion(store, op("t2", "乙", "A", "score", 90))
    service.submit_adjudication(store, {
        "project_id": "A", "field": "score", "value": 89,
        "decided_by": "裁决员", "seen_seq": 2})
    service.submit_opinion(store, op("t3", "丙", "A", "score", 91))  # 裁决后新增意见
    service.commit_merge(store)
    result = merged_of(store)
    assert result["disputes"][0]["status"] == "open"  # 旧裁决失效，争议重开
    assert result["weights"]["paused"][0]["id"] == "A"
    st = service.status(store)
    assert [a["valid"] for a in st["adjudications"]] == [False]  # 失效裁决保留可追责


def test_stale_adjudication_rejected(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    service.submit_opinion(store, op("t2", "乙", "A", "score", 90))
    with pytest.raises(DomainError) as err:
        service.submit_adjudication(store, {
            "project_id": "A", "field": "score", "value": 89,
            "decided_by": "裁决员", "seen_seq": 1})
    assert err.value.code == "STALE_VERSION"


def test_adjudication_requires_opinions(store):
    with pytest.raises(DomainError) as err:
        service.submit_adjudication(store, {
            "project_id": "A", "field": "score", "value": 89,
            "decided_by": "裁决员", "seen_seq": 0})
    assert err.value.code == "NO_OPINIONS"


# --- 收件幂等与校验 ---

def test_opinion_retry_not_duplicated(store):
    first = service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    second = service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    assert first["seq"] == second["seq"] and second["duplicate"] is True
    assert len(store.snapshot()["opinions"]) == 1


def test_token_conflict_rejected(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    with pytest.raises(DomainError) as err:
        service.submit_opinion(store, op("t1", "甲", "A", "score", 90))
    assert err.value.code == "DUPLICATE_CONFLICT"


def test_unknown_baseline_rejected(store):
    with pytest.raises(DomainError) as err:
        service.submit_opinion(store, op("t1", "甲", "A", "score", 88, base="v9"))
    assert err.value.code == "UNKNOWN_BASELINE"


def test_invalid_field_and_id_rejected(store):
    with pytest.raises(DomainError):
        service.submit_opinion(store, op("t1", "甲", "A", "nosuch", 1))
    with pytest.raises(DomainError):
        service.submit_opinion(store, op("t2", "甲", "A", "id", "X"))


def test_type_mismatch_rejected(store):
    with pytest.raises(DomainError) as err:
        service.submit_opinion(store, op("t1", "甲", "A", "score", "高分"))
    assert err.value.code == "INVALID_VALUE"


def test_baseline_version_conflict(store):
    with pytest.raises(DomainError) as err:
        service.submit_baseline(store, {"version": "v1", "projects": [{"id": "Z"}]})
    assert err.value.code == "DUPLICATE_CONFLICT"
    again = service.submit_baseline(store, BASE)  # 同内容重登幂等
    assert again["duplicate"] is True


# --- 提交原子性、恢复与回读 ---

def test_merge_idempotent_reuses_version(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    first = service.commit_merge(store)
    second = service.commit_merge(store)
    assert first["version"] == second["version"]
    assert second["reused"] is True


def test_failure_keeps_previous_version(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    assert service.commit_merge(store)["version"] == 1
    service.submit_opinion(store, op("t2", "乙", "A", "weight", "0"))
    service.submit_opinion(store, op("t3", "丙", "C", "weight", "0"))  # 参与组权重全零
    with pytest.raises(DomainError) as err:
        service.commit_merge(store)
    assert err.value.code == "ZERO_WEIGHT_GROUP"
    assert store.current()["version"] == 1  # 旧版保留
    st = service.status(store)
    assert st["dirty"] is True and st["pending_error"] == "ZERO_WEIGHT_GROUP"


def test_recovery_after_restart(store, tmp_path):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88))
    service.commit_merge(store)
    reopened = ReviewStore(str(tmp_path / "review.db"))  # 模拟中断后重开
    assert reopened.current()["version"] == 1
    assert len(reopened.snapshot()["opinions"]) == 1
    assert service.status(reopened)["opinions"][0]["value"] == 88


def test_readback_provenance(store):
    service.submit_opinion(store, op("t1", "甲", "A", "score", 88, note="复查卷宗"))
    service.submit_opinion(store, op("t2", "乙", "C", "weight", "3"))
    service.commit_merge(store)
    st = service.status(store)
    opinion = st["opinions"][0]
    assert opinion["reviewer"] == "甲" and opinion["note"] == "复查卷宗"
    assert opinion["base_version"] == "v1" and opinion["base_value"] == 82  # 按原材料追责
    sources = {p["id"]: p["sources"] for p in st["merged"]}
    assert sources["A"]["score"] == "opinion:甲"
    assert sources["C"]["weight"] == "opinion:乙"
    weights = {p["id"]: p for p in st["weights"]["participants"]}
    assert weights["C"]["weight_source"] == "opinion:乙"
    assert weights["A"]["weight_source"] == "baseline"
    assert st["baselines"][0]["active"] is True


def test_merge_deterministic_regardless_of_order(tmp_path):
    def build(order):
        s = ReviewStore(str(tmp_path / ("d" + str(order[0]) + ".db")))
        service.submit_baseline(s, BASE)
        for i in order:
            service.submit_opinion(s, op(f"t{i}", f"复核员{i}", "A", "score", 80 + i))
        service.submit_adjudication(s, {"project_id": "A", "field": "score", "value": 85,
                                        "decided_by": "裁决员", "seen_seq": 3})
        service.commit_merge(s)
        return s.current()["result"]
    assert build([1, 2, 3]) == build([3, 1, 2])  # 收件顺序不影响合并结果


# --- 纯函数层 ---

def test_core_merge_uses_m2_rules():
    merged, _ = core.merge_projects(BASE["projects"], [], [])
    by_id = {p["id"]: p for p in merged}
    assert by_id["B"]["eligible"] is False and by_id["B"]["reason"] == "BLACKLIST"
    assert by_id["C"]["eligible"] is True and by_id["C"]["reason"] == "RURAL"


def test_core_weights_all_paused():
    merged = [{"id": "A", "values": {}, "sources": {}, "eligible": True,
               "reason": "RURAL", "disputed_fields": ["score"]}]
    weights = core.compute_weights(merged)
    assert weights["reason"] == "ALL_PAUSED" and weights["participants"] == []


# --- API 全流程 ---

def test_api_flow(client):
    assert client.post("/api/review/baselines", json=BASE).status_code == 200
    r = client.post("/api/review/opinions", json=op("t1", "甲", "A", "score", 88))
    assert r.status_code == 200 and r.json()["duplicate"] is False
    r = client.post("/api/review/opinions", json=op("t1", "甲", "A", "score", 88))
    assert r.json()["duplicate"] is True  # 重试不重复收件
    r = client.post("/api/review/opinions", json=op("t2", "乙", "A", "score", 90))
    assert r.status_code == 200
    r = client.post("/api/review/opinions", json=op("t3", "丙", "A", "score", 90, base="v9"))
    assert r.status_code == 422 and r.json()["error"] == "UNKNOWN_BASELINE"
    first = client.post("/api/review/merge").json()
    assert first["open_disputes"] == 1 and first["paused"] == 1
    again = client.post("/api/review/merge").json()
    assert again["version"] == first["version"] and again["reused"] is True
    r = client.post("/api/review/adjudications",
                    json={"project_id": "A", "field": "score", "value": 89,
                          "decided_by": "裁决员", "seen_seq": 2})
    assert r.status_code == 200
    done = client.post("/api/review/merge").json()
    assert done["version"] > first["version"] and done["open_disputes"] == 0
    st = client.get("/api/review/status").json()
    assert st["dirty"] is False and len(st["opinions"]) == 2
    assert st["disputes"][0]["status"] == "resolved"
    assert st["weights"]["share_sum"] == "1"
    page = client.get("/review")
    assert page.status_code == 200 and "panel" in page.text
