"""공문 본문에서 요지·해야 할 일·일정·제출처 읽기."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import gongmun_facts as gf
from app.db import Database

BODY = """수신 각 부서장
제목 2025년 적극행정 실행계획 제출 요청
1. 관련: 기획과-1234(2025. 3. 2.)
2. 2025년 적극행정 실행계획을 수립하고자 하오니 부서별 실행계획을 작성하여 제출하여 주시기 바랍니다.
3. 제출 기한: 2025. 4. 15.(화)까지
4. 제출처: 기획과 이메일 plan@example.go.kr 로 제출하여 주시기 바랍니다.
붙임 1. 실행계획 서식 1부. 끝.
"""


def by_kind(facts):
    out = {}
    for f in facts:
        out.setdefault(f.kind, []).append(f)
    return out


def test_rules_read_summary_todo_schedule_submit():
    facts = by_kind(gf.extract_rules(BODY, 2025))
    assert "실행계획을 수립하고자" in facts["summary"][0].text
    assert any("작성하여 제출" in f.text for f in facts["todo"])
    assert facts["schedule"][0].due == "2025-04-15"
    assert any("plan@example.go.kr" in f.text for f in facts["submit"])
    # 근거는 본문에 실제로 있는 문장이다.
    flat = " ".join(BODY.split())
    assert all(" ".join(f.evidence.split()) in flat for f in gf.extract_rules(BODY, 2025))


def test_month_day_uses_document_year_only_when_known():
    text = "1. 회의를 개최하오니 참석하여 주시기 바랍니다.\n2. 일시: 9월 12일 14시"
    assert by_kind(gf.extract_rules(text, 2024))["schedule"][0].due == "2024-09-12"
    assert by_kind(gf.extract_rules(text, None))["schedule"][0].due is None


class FakeClient:
    def __init__(self, facts):
        self.facts = facts

    def generate_json(self, prompt, schema, num_predict=0):
        return {"facts": self.facts}, None, ""


def test_model_facts_without_grounding_are_dropped():
    client = FakeClient([
        {"kind": "todo", "text": "서식 제출", "evidence": "부서별 실행계획을 작성하여 제출하여 주시기 바랍니다."},
        {"kind": "todo", "text": "지어낸 일", "evidence": "본문에 없는 문장입니다 정말로."},
    ])
    out = gf.extract_with_model(BODY, client, 2025)
    assert [f.text for f in out] == ["서식 제출"]


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


def test_analyze_bodies_saves_and_skips_unchanged(db):
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES ('D:/x', 'local')").lastrowid
    doc = db.con.execute(
        "INSERT INTO documents (source_id, path, filename, ext, hash, eff_year) VALUES (?, 'D:/x/b.pdf', "
        "'[본문] 적극행정 실행계획 제출 요청.pdf', '.pdf', 'h1', 2025)", (source,)).lastrowid
    db.con.execute("INSERT INTO document_sections(doc_id, ordinal, kind, locator, text) VALUES (?, 0, 'paragraph', 'p1', ?)", (doc, BODY))
    assert gf.body_doc_ids(db) == [doc]
    assert gf.analyze_bodies(db) == 1
    assert gf.analyze_bodies(db) == 0
    kinds = {f["kind"] for f in gf.facts_for_docs(db, [doc])}
    assert {"summary", "todo", "schedule"} <= kinds
    # 사람이 고친 항목은 다시 읽어도 남는다.
    db.con.execute("INSERT INTO official_facts(doc_id, kind, text, decided_by) VALUES (?, 'todo', '내가 추가', 'user')", (doc,))
    gf.analyze_document(db, doc)
    assert any(f["text"] == "내가 추가" for f in gf.facts_for_docs(db, [doc]))
