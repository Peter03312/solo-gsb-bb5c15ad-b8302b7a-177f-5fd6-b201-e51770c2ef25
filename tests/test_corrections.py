"""本轮功能测试: 已交评语的更正 (会务方发起 + 评审人提交)。

覆盖需求:
- 会务方凭密钥指定当前发布序号、论文、评审人与非空原因发起更正请求;
  仅允许当前仍分配、已确认且已交评语的槽位, 旧序号/已移出槽位/未确认/未交评语拒绝;
- 更正请求发起即令该稿既有作者反馈访问码失效; 更正后按现有完整性规则重新发布快照;
- 评审人凭本人凭据查看自己的待更正任务 (看不到他人的),
  以请求对应的发布序号和原评语收据提交新 1~5 整数评分与非空评语;
- 首份有效更正生成新收据并保留原评语供会务方追溯;
  同内容重试返回更正收据 (幂等), 异内容 409 冲突;
- 发布序号变化后待更正请求失效; 补位保留槽位沿用更正后的评语, 移出则归档;
- 既有正式评语首提交接口的一次提交及冲突语义不变; 评审人不得查看他人评语。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-corrections-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "feedback_snapshots", "reviewer_status_changes", "institution_merges",
            "review_corrections",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution,
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid="P1", topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, pid, decision="confirm", reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def submit_review(client, rid, pid, serial, score, comment):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


def backfill_publish(client):
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"], dry
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def request_correction(client, pid, rid, serial, reason="评语需更正"):
    return client.post(
        f"/papers/{pid}/review-corrections",
        headers=ORG,
        json={"paper_id": pid, "reviewer_id": rid, "serial": serial, "reason": reason},
    )


def submit_correction(client, rid, pid, serial, receipt, score, comment):
    return client.post(
        f"/reviewer/review-corrections/{pid}",
        headers=reviewer_headers(rid),
        json={
            "paper_id": pid, "serial": serial, "receipt": receipt,
            "score": score, "comment": comment,
        },
    )


def setup_p1(client, reviewers=("R1", "R2", "R3", "R4")):
    """发布 P1 -> 两槽位确认 -> 各交一份评语。返回 (pub, rid_a, rid_b, receipt_a, receipt_b)。"""
    for rid in reviewers:
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    ra = submit_review(client, rid_a, "P1", serial, 4, "甲的原评语").json()
    rb = submit_review(client, rid_b, "P1", serial, 2, "乙的原评语").json()
    return pub, rid_a, rid_b, ra["receipt"], rb["receipt"]


# ------------------------------------------------------------ 发起更正请求

def test_organizer_can_request_correction(client):
    pub, rid_a, _, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    rev_before = client.get("/meta", headers=ORG).json()["revision"]

    r = request_correction(client, "P1", rid_a, serial, "评分与评语明显不符, 请复核")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True
    assert j["serial"] == serial and j["reviewer_id"] == rid_a
    assert j["reason"] == "评分与评语明显不符, 请复核"
    assert j["original"]["receipt"] == receipt_a
    assert j["original"]["score"] == 4 and j["original"]["comment"] == "甲的原评语"
    # 更正请求不推进资料修订号与发布序号
    meta = client.get("/meta", headers=ORG).json()
    assert meta["revision"] == rev_before
    assert meta["published"]["serial"] == serial


def test_request_requires_organizer_key(client):
    pub, rid_a, _, _, _ = setup_p1(client)
    assert client.post(
        "/papers/P1/review-corrections",
        json={"paper_id": "P1", "reviewer_id": rid_a, "serial": pub["serial"], "reason": "x"},
    ).status_code == 401
    assert client.get("/papers/P1/review-corrections").status_code == 401


def test_request_rejects_invalid_targets(client):
    pub, rid_a, rid_b, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    # 路径与请求体论文编号不一致 -> 422
    assert client.post(
        "/papers/P1/review-corrections", headers=ORG,
        json={"paper_id": "P9", "reviewer_id": rid_a, "serial": serial, "reason": "x"},
    ).status_code == 422
    # 空白原因 -> 422
    assert request_correction(client, "P1", rid_a, serial, "   ").status_code == 422
    # 过期/超前序号 -> 409 (serial=1 时序号 0 触发参数校验 422; 过期场景见专测)
    assert request_correction(client, "P1", rid_a, serial + 1).status_code == 409
    assert request_correction(client, "P1", rid_a, 0).status_code == 422
    # 不存在的论文 -> 404
    assert request_correction(client, "NOPE", rid_a, serial).status_code == 404
    # 未分配给该论文的评审人 (已移出/不存在槽位) -> 404
    assert request_correction(client, "P1", "R4", serial).status_code == 404


def test_request_rejects_unconfirmed_or_unsubmitted_slots(client):
    for rid in ("R1", "R2", "R3"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    # pending 槽位 -> 409
    assert request_correction(client, "P1", rid_a, serial).status_code == 409
    # 已回避槽位 -> 409
    decide(client, rid_b, "P1", "recuse", reason="利益冲突")
    assert request_correction(client, "P1", rid_b, serial).status_code == 409
    # 已确认但未交评语 -> 409
    decide(client, rid_a, "P1", "confirm")
    assert request_correction(client, "P1", rid_a, serial).status_code == 409


def test_request_without_publication_is_404(client):
    add_reviewer(client, "R1", "I1")
    add_paper(client)
    assert request_correction(client, "P1", "R1", 1).status_code == 404


def test_request_with_stale_serial_after_republish_is_409(client):
    pub, rid_a, _, _, _ = setup_p1(client)
    # 普通重新发布推进发布序号
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert request_correction(client, "P1", rid_a, pub["serial"]).status_code == 409


def test_duplicate_request_idempotent_same_reason_conflict_on_different(client):
    pub, rid_a, _, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    first = request_correction(client, "P1", rid_a, serial, "请复核评分").json()
    assert first["changed"] is True
    # 同原因重复提交 -> 幂等
    again = request_correction(client, "P1", rid_a, serial, "  请复核评分  ")
    assert again.status_code == 200
    j = again.json()
    assert j["changed"] is False
    assert j["requested_at"] == first["requested_at"]
    assert j["original"]["receipt"] == receipt_a
    # 异原因 -> 409
    assert request_correction(client, "P1", rid_a, serial, "另一个原因").status_code == 409


# ------------------------------------------------------------ 评审人查看待更正任务

def test_reviewer_sees_only_own_pending_tasks(client):
    pub, rid_a, rid_b, receipt_a, receipt_b = setup_p1(client)
    serial = pub["serial"]
    request_correction(client, "P1", rid_a, serial, "甲请复核")
    request_correction(client, "P1", rid_b, serial, "乙请复核")

    view_a = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_a))
    assert view_a.status_code == 200
    j = view_a.json()
    assert j["serial"] == serial
    assert len(j["corrections"]) == 1
    task = j["corrections"][0]
    assert task["paper_id"] == "P1" and task["serial"] == serial
    assert task["reason"] == "甲请复核"
    assert task["original"]["receipt"] == receipt_a
    assert task["original"]["comment"] == "甲的原评语"
    # 不含他人评语与作者机构
    assert "乙" not in str(j) and "Author-P1" not in str(j) and "institution" not in str(j)

    view_b = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_b)).json()
    assert [t["original"]["receipt"] for t in view_b["corrections"]] == [receipt_b]
    # 未发起请求的评审人看不到任务
    assert client.get(
        "/reviewer/review-corrections", headers=reviewer_headers("R3")
    ).json()["corrections"] == []


def test_reviewer_corrections_require_credentials(client):
    pub, rid_a, _, _, _ = setup_p1(client)
    request_correction(client, "P1", rid_a, pub["serial"])
    assert client.get("/reviewer/review-corrections").status_code == 401
    assert client.get(
        "/reviewer/review-corrections",
        headers={"X-Reviewer-Id": rid_a, "X-Reviewer-Credential": "wrong"},
    ).status_code == 401


def test_reviewer_corrections_empty_without_publication(client):
    add_reviewer(client, "R1", "I1")
    j = client.get("/reviewer/review-corrections", headers=reviewer_headers("R1")).json()
    assert j == {"reviewer_id": "R1", "serial": None, "corrections": []}


# ------------------------------------------------------------ 提交更正

def test_submit_correction_generates_new_receipt_and_keeps_original(client):
    pub, rid_a, _, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    request_correction(client, "P1", rid_a, serial, "请复核")
    rev_before = client.get("/meta", headers=ORG).json()["revision"]

    r = submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "更正后的评语")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True
    assert j["receipt"].startswith("rvw-") and j["receipt"] != receipt_a
    assert j["original_receipt"] == receipt_a
    assert j["score"] == 5 and j["comment"] == "更正后的评语"
    # 更正提交不推进资料修订号与发布序号
    meta = client.get("/meta", headers=ORG).json()
    assert meta["revision"] == rev_before and meta["published"]["serial"] == serial

    # 评审人本人视图: 当前有效任务上是更正后的评语与新收据
    mine = client.get("/reviewer/reviews", headers=reviewer_headers(rid_a)).json()
    assert mine["reviews"][0]["receipt"] == j["receipt"]
    assert mine["reviews"][0]["comment"] == "更正后的评语"
    assert mine["reviews"][0]["score"] == 5

    # 会务方进度视图: 当前槽位为更正后内容
    prog = client.get("/papers/P1/reviews", headers=ORG).json()
    slot = next(s for s in prog["slots"] if s["reviewer_id"] == rid_a)
    assert slot["review"]["receipt"] == j["receipt"]
    assert slot["review"]["comment"] == "更正后的评语"

    # 会务方追溯: 原评语完整保留, 状态 fulfilled
    trace = client.get("/papers/P1/review-corrections", headers=ORG).json()
    assert trace["current_serial"] == serial
    assert len(trace["corrections"]) == 1
    c = trace["corrections"][0]
    assert c["status"] == "fulfilled"
    assert c["original"] == {
        "score": 4, "comment": "甲的原评语",
        "receipt": receipt_a, "submitted_at": c["original"]["submitted_at"],
    }
    assert c["correction"]["receipt"] == j["receipt"]
    assert c["correction"]["score"] == 5 and c["correction"]["comment"] == "更正后的评语"

    # 待更正任务清空
    assert client.get(
        "/reviewer/review-corrections", headers=reviewer_headers(rid_a)
    ).json()["corrections"] == []


def test_correction_retry_idempotent_and_conflict(client):
    pub, rid_a, _, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    request_correction(client, "P1", rid_a, serial)
    first = submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "更正后评语").json()

    # 同内容重试 (含首尾空白差异) -> 原更正收据, changed=false
    retry = submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "  更正后评语 ")
    assert retry.status_code == 200
    j = retry.json()
    assert j["changed"] is False
    assert j["receipt"] == first["receipt"]
    assert j["corrected_at"] == first["corrected_at"]

    # 异内容 -> 409, 已存更正不变
    assert submit_correction(client, rid_a, "P1", serial, receipt_a, 3, "改成别的").status_code == 409
    assert submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "文字不同").status_code == 409
    again = submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "更正后评语").json()
    assert again["receipt"] == first["receipt"]


def test_submit_correction_validation_and_targeting(client):
    pub, rid_a, rid_b, receipt_a, receipt_b = setup_p1(client)
    serial = pub["serial"]
    request_correction(client, "P1", rid_a, serial)
    # 路径/请求体论文不一致 -> 422
    assert client.post(
        "/reviewer/review-corrections/P1", headers=reviewer_headers(rid_a),
        json={"paper_id": "P9", "serial": serial, "receipt": receipt_a, "score": 5, "comment": "x"},
    ).status_code == 422
    # 非法评分 -> 422
    for bad in (0, 6, 2.5, "5"):
        assert submit_correction(client, rid_a, "P1", serial, receipt_a, bad, "x").status_code == 422
    # 空白评语 -> 422
    assert submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "   ").status_code == 422
    # 收据与请求不符 -> 409
    assert submit_correction(client, rid_a, "P1", serial, receipt_b, 5, "x").status_code == 409
    assert submit_correction(client, rid_a, "P1", serial, "rvw-nonexistent", 5, "x").status_code == 409
    # 本人无待更正任务 -> 404 (rid_b 未被请求更正)
    assert submit_correction(client, rid_b, "P1", serial, receipt_b, 5, "x").status_code == 404
    # 过期/超前序号 -> 409
    assert submit_correction(client, rid_a, "P1", serial + 1, receipt_a, 5, "x").status_code == 409
    # 错误凭据 -> 401
    assert client.post(
        "/reviewer/review-corrections/P1",
        headers={"X-Reviewer-Id": rid_a, "X-Reviewer-Credential": "bad"},
        json={"paper_id": "P1", "serial": serial, "receipt": receipt_a, "score": 5, "comment": "x"},
    ).status_code == 401
    # 全部被拒后任务仍开放, 可正常提交
    assert submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "有效更正").status_code == 200


def test_disabled_reviewer_gets_403(client):
    pub, rid_a, _, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    request_correction(client, "P1", rid_a, serial)
    rev = client.get("/meta", headers=ORG).json()["revision"]
    client.post(
        f"/reviewers/{rid_a}/status", headers=ORG,
        json={"reviewer_id": rid_a, "base_revision": rev, "active": False, "reason": "暂停"},
    )
    assert client.get(
        "/reviewer/review-corrections", headers=reviewer_headers(rid_a)
    ).status_code == 403
    assert submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "x").status_code == 403


# ------------------------------------------------------------ 反馈访问码失效与重新发布

def test_correction_request_invalidates_feedback_code_and_republish_works(client):
    pub, rid_a, rid_b, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    snap = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    ).json()
    code = snap["access_code"]
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200

    # 发起更正请求 -> 既有访问码立即失效 (统一 404)
    request_correction(client, "P1", rid_a, serial, "评语需更正")
    r = client.get(f"/feedback-snapshots/{code}")
    assert r.status_code == 404
    assert r.json() == client.get("/feedback-snapshots/fbk-does-not-exist").json()
    # 会务方追溯视图中旧版标记为失效
    versions = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert versions[0]["active"] is False

    # 提交更正后, 会务方按现有完整性规则 (同序号, 收据已变) 重新发布 -> 新版本新码
    corrected = submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "更正后的评语").json()
    snap2 = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    ).json()
    assert snap2["changed"] is True
    assert snap2["version"] == snap["version"] + 1
    assert snap2["access_code"] != code
    # 新码可读且为更正后内容; 旧码仍 404
    view = client.get(f"/feedback-snapshots/{snap2['access_code']}").json()
    assert view["reviews"][0]["score"] == 5
    assert view["reviews"][0]["comment"] == "更正后的评语"
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    versions = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"]
    assert [(v["version"], v["active"]) for v in versions] == [(1, False), (2, True)]


def test_snapshot_without_correction_unaffected(client):
    """未发起更正时, 快照读取与幂等重发的既有语义不变。"""
    pub, _, _, _, _ = setup_p1(client)
    serial = pub["serial"]
    snap = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    ).json()
    again = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    ).json()
    assert again["changed"] is False and again["access_code"] == snap["access_code"]
    assert client.get(f"/feedback-snapshots/{snap['access_code']}").status_code == 200


# ------------------------------------------------------------ 发布序号变化 -> 待更正请求失效

def test_pending_request_expires_on_serial_change(client):
    pub, rid_a, _, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    request_correction(client, "P1", rid_a, serial)
    # 普通重新发布推进发布序号
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    pub2 = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev}).json()
    assert pub2["serial"] == serial + 1
    # 待更正任务从评审人视图消失
    assert client.get(
        "/reviewer/review-corrections", headers=reviewer_headers(rid_a)
    ).json()["corrections"] == []
    # 旧序号提交 -> 409 (请求已失效)
    assert submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "x").status_code == 409
    # 会务方追溯中状态为 expired
    trace = client.get("/papers/P1/review-corrections", headers=ORG).json()
    assert trace["corrections"][0]["status"] == "expired"
    assert trace["corrections"][0]["correction"] is None


# ------------------------------------------------------------ 补位: 沿用更正后的评语 / 移出归档

def test_backfill_carries_corrected_review(client):
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client)
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "recuse", reason="冲突")
    old = submit_review(client, rid_a, "P1", serial, 3, "保留槽位的原评语").json()
    # 会务方发起更正, 评审人提交更正
    request_correction(client, "P1", rid_a, serial, "请复核")
    corrected = submit_correction(client, rid_a, "P1", serial, old["receipt"], 5, "更正后的评语").json()

    out = backfill_publish(client)
    assert out["carried_reviews"] == 1
    assert rid_a in out["plan"]["P1"]
    # 保留槽位沿用更正后的评语 (新收据, 更正后内容)
    view = client.get("/reviewer/reviews", headers=reviewer_headers(rid_a)).json()
    assert view["serial"] == out["serial"]
    assert view["reviews"][0]["receipt"] == corrected["receipt"]
    assert view["reviews"][0]["comment"] == "更正后的评语"
    prog = client.get("/papers/P1/reviews", headers=ORG).json()
    slot = next(s for s in prog["slots"] if s["reviewer_id"] == rid_a)
    assert slot["review"]["receipt"] == corrected["receipt"]
    assert slot["review"]["score"] == 5


def test_removed_slot_correction_archived_and_original_preserved(client):
    # 通过机构变更使已确认槽位在补位中被移出
    for rid in ("R1", "R2", "R3"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client, institutions=["Author-A"])
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"]["P1"]
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    old = submit_review(client, rid_b, "P1", serial, 3, "将被移出的原评语").json()
    request_correction(client, "P1", rid_b, serial, "请复核")
    corrected = submit_correction(client, rid_b, "P1", serial, old["receipt"], 4, "更正后的评语").json()

    # rid_b 机构变得与 rid_a 相同 -> 固定对失效, rid_b 被移出, R3 补入
    client.put(
        f"/reviewers/{rid_b}", headers=ORG,
        json={"credential": f"cred-{rid_b}", "topics": ["AI"],
              "institution": f"Inst-{rid_a}", "capacity": 3, "avoid_papers": []},
    )
    out = backfill_publish(client)
    assert rid_b not in out["plan"]["P1"]
    # 移出槽位的更正后评语进入归档 (仅供会务方追溯), 不计入新方案进度
    prog = client.get("/papers/P1/reviews", headers=ORG).json()
    assert prog["progress"]["submitted"] == 0
    archived = [(a["serial"], a["reviewer_id"], a["receipt"]) for a in prog["archived_reviews"]]
    assert (serial, rid_b, corrected["receipt"]) in archived
    # 更正记录仍可追溯 (状态 expired, 原评语保留)
    trace = client.get("/papers/P1/review-corrections", headers=ORG).json()
    assert trace["corrections"][0]["status"] == "fulfilled"
    assert trace["corrections"][0]["original"]["comment"] == "将被移出的原评语"


# ------------------------------------------------------------ 首提交接口语义不变

def test_first_submission_endpoint_semantics_unchanged_after_correction(client):
    pub, rid_a, _, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    request_correction(client, "P1", rid_a, serial)
    corrected = submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "更正后的评语").json()

    # 首提交接口: 与当前 (更正后) 评语同内容 -> 幂等返回更正收据
    same = submit_review(client, rid_a, "P1", serial, 5, "更正后的评语")
    assert same.status_code == 200
    assert same.json()["changed"] is False
    assert same.json()["receipt"] == corrected["receipt"]
    # 内容不同 (含已被替换的原评语) -> 409 冲突
    assert submit_review(client, rid_a, "P1", serial, 4, "甲的原评语").status_code == 409
    assert submit_review(client, rid_a, "P1", serial, 1, "别的内容").status_code == 409


def test_second_correction_round_targets_corrected_review(client):
    """更正完成后可再次发起更正: 新请求针对更正后的评语 (其收据为新的原收据)。"""
    pub, rid_a, _, receipt_a, _ = setup_p1(client)
    serial = pub["serial"]
    request_correction(client, "P1", rid_a, serial, "第一次更正")
    first = submit_correction(client, rid_a, "P1", serial, receipt_a, 5, "第一次更正后").json()

    r2 = request_correction(client, "P1", rid_a, serial, "第二次更正")
    assert r2.status_code == 200
    assert r2.json()["original"]["receipt"] == first["receipt"]
    # 第二次更正须携带第一次更正的收据
    assert submit_correction(client, rid_a, "P1", serial, receipt_a, 4, "x").status_code == 409
    second = submit_correction(client, rid_a, "P1", serial, first["receipt"], 4, "第二次更正后")
    assert second.status_code == 200
    assert second.json()["receipt"] not in (receipt_a, first["receipt"])
    # 追溯: 两轮更正完整保留
    trace = client.get("/papers/P1/review-corrections", headers=ORG).json()["corrections"]
    assert [c["status"] for c in trace] == ["fulfilled", "fulfilled"]
    assert trace[0]["original"]["receipt"] == receipt_a
    assert trace[1]["original"]["receipt"] == first["receipt"]
    assert trace[1]["correction"]["comment"] == "第二次更正后"
