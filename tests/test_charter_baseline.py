"""사용자가 입력한 분장이 기준일 때 공문 업무가 분장 항목에 맞춰진다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.discover import discover
from app.core import charter, gongmun_facts
from app.db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


def _body(db, source, name, text):
    doc = db.con.execute(
        "INSERT INTO documents (source_id, path, filename, ext, hash, eff_year, parse_status) "
        "VALUES (?, ?, ?, '.pdf', ?, 2025, 'ok')", (source, f"D:/전임자/{name}", name, name)).lastrowid
    db.con.execute("INSERT INTO document_sections(doc_id, ordinal, kind, locator, text) VALUES (?, 0, 'paragraph', 'p1', ?)",
                   (doc, text))
    return doc


def seed(db):
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES ('D:/전임자', 'local')").lastrowid
    text = ("제목 일일 상담 현황 점검\n1. 민원응대 품질 향상을 위하여 상담 현황을 점검하고자 합니다.\n"
            "2. 각 부서는 현황을 작성하여 제출하여 주시기 바랍니다.")
    docs = [_body(db, source, "[본문] 일일 상담 현황 점검 요청.pdf", text),
            _body(db, source, "[본문] 일일 상담 현황 점검 결과 보고.pdf", text)]
    gongmun_facts.analyze_bodies(db)
    return docs


def names(db):
    return [r["name"] for r in db.con.execute("SELECT name FROM tasks WHERE not_a_task = 0")]


def test_official_task_takes_user_charter_name_by_body_content(db):
    seed(db)
    charter.save_user_items(db, charter.parse_user_text("민원응대 개선\n예산 편성"))
    discover(db)
    assert "민원응대 개선" in names(db)
    assert "일일 상담 현황 점검" not in names(db)
    cmp = json.loads(db.get_meta("charter_comparison"))
    assert [m["name"] for m in cmp["matched"]] == ["민원응대 개선"]
    assert [c["name"] for c in cmp["charter_only"]] == ["예산 편성"]


def test_without_user_charter_names_stay_as_before(db):
    seed(db)
    discover(db)
    assert "일일 상담 현황 점검" in names(db)


def test_brief_shows_what_the_letter_body_says(db):
    from app.core import notebook_tasks
    seed(db)
    charter.save_user_items(db, charter.parse_user_text("민원응대 개선"))
    discover(db)
    task_id = db.con.execute("SELECT id FROM tasks WHERE name = '민원응대 개선'").fetchone()["id"]
    brief = notebook_tasks.brief_markdown(db, task_id)
    assert "공문 본문에서 읽은 내용" in brief
    assert "해야 할 일" in brief and "[본문] 일일 상담 현황 점검 요청.pdf" in brief


class _Health:
    generation_ready = True


class FakeLLM:
    """분류 판정만 흉내 낸다. 고른 번호를 정해 둔다."""
    embed_model = "fake"
    gen_model = "fake"

    def __init__(self, choice, confidence="high", quote=None):
        self.choice, self.confidence, self.quote = choice, confidence, quote
        self.calls = 0

    def health(self):
        return _Health()

    def generate_json(self, prompt, schema, num_predict=0):
        self.calls += 1
        return {"choice": self.choice, "confidence": self.confidence,
                "evidence": self.quote or "일일 상담 현황 점검"}, None, ""


def _seed_unlike(db):
    """분장 항목 이름과 공문 제목이 낱말로 하나도 겹치지 않는다."""
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES ('D:/전임자', 'local')").lastrowid
    text = "제목 현황 확인\n1. 각 부서는 현황을 작성하여 제출하여 주시기 바랍니다."
    for name in ("[본문] 일일 상담 현황 점검 요청.pdf", "[본문] 일일 상담 현황 점검 결과 보고.pdf"):
        _body(db, source, name, text)
    gongmun_facts.analyze_bodies(db)
    charter.save_user_items(db, charter.parse_user_text("고객 응대 혁신\n예산 편성"))


def test_model_matches_what_rules_cannot(db):
    _seed_unlike(db)
    llm = FakeLLM(choice=1)
    discover(db, llm)
    assert "고객 응대 혁신" in names(db) and "일일 상담 현황 점검" not in names(db)
    description = db.con.execute("SELECT description FROM tasks WHERE name = '고객 응대 혁신'").fetchone()[0]
    assert "AI가 분장 항목" in description
    calls = llm.calls
    discover(db, llm)                       # 같은 입력이면 다시 묻지 않는다
    assert llm.calls == calls


@pytest.mark.parametrize("llm", [
    FakeLLM(choice=0),                               # 해당 없음
    FakeLLM(choice=9),                               # 목록 밖 번호
    FakeLLM(choice=1, confidence="low"),             # 확신 낮음
    FakeLLM(choice=1, quote="자료에 없는 지어낸 근거 문장"),   # 근거가 자료에 없음
])
def test_bad_model_answers_are_not_applied(db, llm):
    _seed_unlike(db)
    discover(db, llm)
    assert "일일 상담 현황 점검" in names(db) and "고객 응대 혁신" not in [
        n for n in names(db) if db.con.execute(
            "SELECT 1 FROM task_docs td JOIN tasks t ON t.id = td.task_id WHERE t.name = ?", (n,)).fetchone()]


def test_parts_and_rounds_reach_task_docs_brief_and_handover_folder(db, tmp_path):
    from app.core import handover_folder, notebook_tasks
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES (?, 'local')", (str(tmp_path / "자료"),)).lastrowid
    root = tmp_path / "자료"
    for rel in ("예산 편성/2024 하반기 본예산/요구서.txt", "예산 편성/2024 하반기 본예산/집행.txt",
                "예산 편성/결산/a.txt", "예산 편성/결산/b.txt"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("예산 자료", encoding="utf-8")
        doc = db.con.execute(
            "INSERT INTO documents (source_id, path, filename, ext, hash, eff_year, parse_status) "
            "VALUES (?, ?, ?, '.txt', ?, 2024, 'ok')", (source, str(path), path.name, rel)).lastrowid
        db.con.execute("INSERT INTO chunks(doc_id, ordinal, locator, text) VALUES (?, 0, 'p1', '예산 자료')", (doc,))
    charter.save_user_items(db, charter.parse_user_text("예산 편성\n  - 본예산\n  - 결산"))
    discover(db)
    task_id = db.con.execute("SELECT id FROM tasks WHERE name = '예산 편성'").fetchone()["id"]
    rows = db.con.execute("SELECT part, round_key FROM task_docs WHERE task_id = ?", (task_id,)).fetchall()
    assert sorted(r["part"] for r in rows) == ["결산", "결산", "본예산", "본예산"]
    assert {r["round_key"] for r in rows if r["part"] == "본예산"} == {"2024 하반기"}
    assert "세부업무" in notebook_tasks.brief_markdown(db, task_id)
    out = tmp_path / "out"
    handover_folder.export_handover_folder(db, out)
    assert (out / "예산 편성" / "본예산").is_dir() and (out / "예산 편성" / "결산").is_dir()
