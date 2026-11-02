"""자료 단위를 사무분장 항목에 나누는 분류기 (규칙 → 모델 판정 → 인용 맞추기)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai import classify_units as cu
from app.core import charter, gongmun_facts, units
from app.db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    database.source = database.con.execute(
        "INSERT INTO sources (path, kind) VALUES ('D:/전임자', 'local')").lastrowid
    yield database
    database.close()


def doc(db, rel, text="", year=2025):
    doc_id = db.con.execute(
        "INSERT INTO documents (source_id, path, filename, ext, hash, eff_year, parse_status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'ok')",
        (db.source, f"D:/전임자/{rel}", rel.rsplit("/", 1)[-1], Path(rel).suffix, rel, year)).lastrowid
    if text:
        db.con.execute("INSERT INTO document_sections(doc_id, ordinal, kind, locator, text) VALUES (?, 0, 'paragraph', 'p1', ?)",
                       (doc_id, text))
        db.con.execute("INSERT INTO chunks(doc_id, ordinal, locator, text) VALUES (?, 0, 'p1', ?)", (doc_id, text))
    return doc_id


class _Health:
    generation_ready = True


class LLM:
    """고른 항목 이름을 정해 둔 가짜 모델. 항목 이름이 선택지에 있으면 그 번호를 낸다."""
    embed_model = gen_model = "fake"

    def __init__(self, pick=None):
        self.pick, self.calls = pick, 0

    def health(self):
        return _Health()

    def generate_json(self, prompt, schema, num_predict=0):
        self.calls += 1
        listing = prompt.split("[업무 분장]")[1].split("판단 규칙")[0]
        number = 0
        for line in listing.strip().splitlines():
            if self.pick and self.pick in line:
                number = int(line.split(".")[0])
        material = prompt.rsplit("[자료]", 1)[1].strip()
        quote = next((l.split(": ", 1)[1] for l in material.splitlines() if l.startswith("문서 제목")), "")
        return {"choice": number, "confidence": "high", "evidence": quote}, None, ""


def setup(db, charter_text):
    charter.save_user_items(db, charter.parse_user_text(charter_text))


def result(db):
    return {u["label"]: cu.assignments(db)[u["key"]] for u in units.load(db)}


def test_name_rule_assigns_folder_unit_without_model(db):
    setup(db, "예산 편성\n인사 관리")
    doc(db, "예산 편성/본예산/요구서.xlsx", "요구서")
    doc(db, "예산 편성/본예산/집행.xlsx", "집행")
    counts = cu.classify(db)
    a = result(db)["본예산"]
    assert a.item_name == "예산 편성" and a.basis == cu.BASIS_NAME and a.confidence == "high"
    assert counts["item"] == 1 and counts["model_calls"] == 0


def test_unknown_without_model_and_ai_judgement_with_model(db):
    setup(db, "고객 응대 혁신\n예산 편성")
    doc(db, "잡다/상담 현황.hwp", "일일 상담 현황")
    doc(db, "잡다/상담 결과.hwp", "상담 결과")
    cu.classify(db)
    assert result(db)["잡다"].bucket == cu.UNKNOWN
    cu.classify(db, LLM(pick="고객 응대"))
    a = result(db)["잡다"]
    assert (a.item_name, a.basis, a.bucket) == ("고객 응대 혁신", cu.BASIS_AI, "item")
    doc(db, "별건/체육 행사.hwp", "체육 행사")
    doc(db, "별건/체육 결과.hwp", "체육 결과")
    cu.classify(db, LLM(pick=None))
    assert result(db)["별건"].bucket == cu.OUTSIDE        # 모델이 어느 항목도 아니라고 봄
    llm = LLM(pick="고객 응대")
    cu.classify(db, llm)
    assert llm.calls == 0                                # 같은 입력은 다시 묻지 않는다


def test_user_decision_survives_reclassification(db):
    setup(db, "고객 응대 혁신\n예산 편성")
    doc(db, "잡다/상담 현황.hwp", "상담")
    doc(db, "잡다/상담 결과.hwp", "상담")
    cu.classify(db)
    key = units.load(db)[0]["key"]
    cu.set_user_assignment(db, key, "예산 편성")
    cu.classify(db, LLM(pick="고객 응대"))
    a = cu.assignments(db)[key]
    assert (a.item_name, a.decided_by) == ("예산 편성", "user")
    # 항목이 분장에서 사라지면 그 지정은 무효가 된다.
    setup(db, "고객 응대 혁신")
    cu.classify(db, LLM(pick="고객 응대"))
    assert cu.assignments(db)[key].decided_by == "ai"


def test_call_cap_leaves_the_rest_unjudged(db):
    setup(db, "고객 응대 혁신\n예산 편성")
    for n in range(4):
        doc(db, f"f{n}/a.hwp", "x")
        doc(db, f"f{n}/b.hwp", "y")
    llm = LLM(pick="고객 응대")
    counts = cu.classify(db, llm, max_calls=2)
    assert llm.calls == 2 and counts["not_judged"] == 2 and counts["unknown"] == 2


def _record(db, run, number, title, body_doc, doc_hash):
    record = db.con.execute(
        "INSERT INTO source_records(run_id, institution, system_id, document_id, title, document_number) "
        "VALUES (?, 'x', 's', ?, ?, ?)", (run, number, title, number)).lastrowid
    db.con.execute(
        "INSERT INTO source_assets(record_id, asset_id, role, filename, sha256, status, doc_id) "
        "VALUES (?, 'a', 'body', 'body.pdf', ?, 'downloaded', ?)", (record, doc_hash, body_doc))


def test_cited_letters_follow_the_sure_one(db):
    setup(db, "적극행정 실행계획\n예산 편성")
    run = db.con.execute(
        "INSERT INTO collection_runs(collection_key, adapter, institution, system_id, scope) "
        "VALUES ('k', 'a', 'x', 's', '{}')").lastrowid
    first = doc(db, "r/1.pdf", "제목 적극행정 실행계획 제출 요청\n1. 실행계획을 제출하여 주시기 바랍니다.")
    second = doc(db, "r/2.pdf", "제목 회신\n1. 관련: 기획과-1234 호와 관련입니다.\n2. 아래와 같이 회신합니다.")
    _record(db, run, "기획과-1234", "적극행정 실행계획 제출 요청", first, "h1")
    _record(db, run, "회신과-77", "회신", second, "h2")
    cu.classify(db)
    mine = {u["label"]: cu.assignments(db)[u["key"]] for u in units.load(db)}
    assert mine["적극행정 실행계획"].item_name == "적극행정 실행계획"
    cited = mine["회신"]
    assert (cited.item_name, cited.basis) == ("적극행정 실행계획", cu.BASIS_CITE)
