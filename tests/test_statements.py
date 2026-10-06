"""문장으로만 적힌 내용 찾기 — 모델은 고르기만 한다.

지켜야 하는 것
  · 항목의 값은 문서의 문장 그대로다. 모델이 지어낸 번호·갈래는 버린다.
  · '추정'으로 표시되고, 필수 항목의 공백을 닫거나 단계를 막지 않는다.
  · 모델이 없거나 실패해도 규칙 기반 복원은 그대로다.
  · 같은 문장 묶음에는 지난 결과를 다시 쓴다(분석마다 흔들리지 않는다).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core import reconstruction as rc, statements, workcard, workstate as ws  # noqa: E402
from app.db import Database  # noqa: E402

BASIS = date(2025, 9, 10)
CONTRACT = ("2025년 시설물 점검 용역 계약 체결 보고\n작성일: 2025. 5. 12.\n"
            "계약금액: 48,000천원\n납기: 2025. 11. 28.\n"
            "대금은 준공검사를 마친 뒤 30일 이내에 정산한다.")
MEETING = ("2025년 시설물 점검 용역 중간 점검 회의\n지급액: 19,200천원\n"
           "1차 기성금을 지급하였고 잔여 공정을 확인하였다. 다음 회의는 추후 정한다.")
OLD = "2024년 용역 정산 보고\n정산액: 44,100천원\n잔액을 반납 처리하였다."


class FakeClient:
    gen_model = "fake-model"

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.prompts = answer, error, []

    def generate_json(self, prompt, schema, num_predict=400, temperature=0.0):
        self.prompts.append(prompt)
        if self.error:
            return None, self.error, ""
        return self.answer, None, ""


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "project" / "project.db")
    database.init()
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '용역 계약·정산')")
    yield database
    database.close()


def add(db, name, when, text):
    root = Path(db.path).parent.parent / "자료"
    root.mkdir(exist_ok=True)
    doc = db.upsert_document(db.add_source(root), {
        "path": str(root / name), "filename": name, "ext": ".txt", "parse_status": "ok",
        "hash": name, "char_count": len(text), "eff_date": when, "eff_year": int(when[:4]),
        "eff_month": int(when[5:7]), "eff_precision": "day", "eff_date_kind": "body"})
    db.replace_sections(doc, [("paragraph", 1, "1문단", text)])
    db.assign_document(1, doc)
    return doc


@pytest.fixture
def contract(db):
    add(db, "2024_용역_정산_보고.txt", "2024-12-13", OLD)
    add(db, "2025_용역계약_체결보고.txt", "2025-05-12", CONTRACT)
    add(db, "2025_용역_중간점검_회의.txt", "2025-08-20", MEETING)
    return db


def listed(db) -> list[str]:
    return [item["text"] for item in statements.sentences(db, 1)]


def pick(db, *wanted: tuple[str, str]) -> dict:
    """문장 일부로 번호를 찾아 모델의 답을 만든다."""
    texts = listed(db)
    return {"found": [{"n": next(i for i, t in enumerate(texts, 1) if part in t), "type": kind}
                      for part, kind in wanted]}


def found(db) -> list[dict]:
    return [item for item in db.task_items(1) if item["kind"] == rc.STATEMENT]


def test_only_unlabelled_sentences_of_the_newest_period_are_offered(contract):
    texts = listed(contract)
    assert "대금은 준공검사를 마친 뒤 30일 이내에 정산한다." in texts
    assert "1차 기성금을 지급하였고 잔여 공정을 확인하였다" in texts
    assert not any("계약금액" in t or "납기" in t or "작성일" in t for t in texts)   # 규칙의 몫
    assert not any("계약 체결 보고" in t for t in texts)                             # 제목
    assert not any("반납" in t for t in texts)                                       # 지난 회차


def test_template_sentences_repeated_across_documents_are_not_offered(db):
    """실제 모델로 돌렸을 때 어느 문서에나 있는 문장이 갈래를 받아 자리를 채웠다."""
    for month in (7, 8, 9):
        add(db, f"2025_실적보고_{month}월.txt", f"2025-{month:02d}-12",
            f"본 문서는 2025년 {month}월 실적보고 업무 중 취합 단계에서 작성되었다.\n"
            "담당부서는 관련 자료를 취합하여 기한 내 제출하였다."
            + ("\n증감 사유는 전년 동기 대비로 적는다." if month == 9 else ""))
    assert listed(db) == ["증감 사유는 전년 동기 대비로 적는다."]

    # 다른 업무에도 나오는 문장 틀은 기관의 서식 문구다.
    db.con.execute("INSERT INTO tasks(id, name) VALUES (2, '수질통계')")
    other = add(db, "2025_수질통계.txt", "2025-07-12", "증감 사유는 전년 동기 대비로 적는다.\n"
                "시료는 채수한 날 바로 접수한다.")
    db.con.execute("UPDATE task_docs SET task_id = 2 WHERE doc_id = ?", (other,))
    common = statements.common_skeletons(db, [1, 2])
    assert listed(db) == ["증감 사유는 전년 동기 대비로 적는다."]      # 묻지 않으면 그대로
    assert [i["text"] for i in statements.sentences(db, 1, common)] == []
    assert [i["text"] for i in statements.sentences(db, 2, common)] == [
        "시료는 채수한 날 바로 접수한다."]


def test_the_item_is_the_sentence_itself_marked_as_inferred(contract):
    client = FakeClient(pick(contract, ("30일 이내", "deadline"), ("기성금", "progress")))
    totals = rc.restore(contract, BASIS, client=client)

    assert totals["statements"] == 2
    items = {item["content"]: item for item in found(contract)}
    settle = items["대금은 준공검사를 마친 뒤 30일 이내에 정산한다."]
    assert settle["basis_state"] == ws.INFERRED and settle["human_state"] == ws.NONE
    assert settle["slot"].startswith("기한·기간 · ")
    assert settle["extractor"].startswith("fake-model:find_statements@")
    assert settle["evidence"][0]["quote"] == settle["content"]
    assert settle["evidence"][0]["filename"] == "2025_용역계약_체결보고.txt"
    assert workcard.item_label(settle) == "기한·기간"
    # 규칙으로 읽은 항목은 그대로 규칙의 것이다.
    amount = next(i for i in contract.task_items(1) if i["kind"] == rc.AMOUNT)
    assert amount["extractor"] == rc.EXTRACTOR
    # 모델에게는 번호 매긴 문장만 보낸다.
    assert "1. " in client.prompts[0] and "30일 이내에 정산한다" in client.prompts[0]


def test_answers_that_point_outside_the_list_are_dropped(contract):
    count = len(listed(contract))
    client = FakeClient({"found": [
        {"n": 0, "type": "deadline"}, {"n": count + 1, "type": "deadline"},
        {"n": 1, "type": "지어낸갈래"}, {"n": "2", "type": "howto"}, {"n": True, "type": "howto"},
        "문장", {"n": 2, "type": statements.NONE},          # 해당 없음이라고 답한 문장
        {"n": 1, "type": "deadline"}, {"n": 1, "type": "howto"}]})
    rc.restore(contract, BASIS, client=client)
    assert [item["content"] for item in found(contract)] == [listed(contract)[0]]

    for broken in ({}, {"found": "없음"}, []):
        contract.con.execute("DELETE FROM meta WHERE key LIKE 'statements:%'")
        rc.restore(contract, BASIS, client=FakeClient(broken))
        assert found(contract) == []


def test_found_sentences_never_close_a_required_gap_or_touch_steps(contract):
    before = rc.restore(contract, BASIS)
    gaps_before = {gap["gap_key"] for gap in contract.info_gaps(1)}
    client = FakeClient(pick(contract, ("30일 이내", "deadline")))
    after = rc.restore(contract, BASIS, client=client)

    assert {gap["gap_key"] for gap in contract.info_gaps(1)} == gaps_before
    assert "missing:recipient" in gaps_before
    assert (after["items"], after["steps"], after["gaps"]) == (
        before["items"], before["steps"], before["gaps"])
    card = workcard.build(contract, 1)
    assert [item["content"] for item in card.found] == [
        "대금은 준공검사를 마친 뒤 30일 이내에 정산한다."]
    assert all(item["kind"] != rc.STATEMENT for item in card.todo + card.prepare)
    draft = workcard.draft_markdown(contract, 1, BASIS)
    assert "## 문장에서 찾은 내용" in draft and "추정" in draft
    assert "- 기한·기간: 대금은 준공검사를 마친 뒤 30일 이내에 정산한다. [" in draft


def test_without_a_model_nothing_is_added_and_rules_still_work(contract):
    totals = rc.restore(contract, BASIS)
    assert totals["statements"] == 0 and totals["items"] >= 3
    assert found(contract) == []

    totals = rc.restore(contract, BASIS, client=FakeClient(error="연결 오류"))
    assert totals["statements"] == 0 and found(contract) == []
    assert workcard.build(contract, 1).found == []


def test_the_same_sentences_reuse_the_last_answer(contract):
    client = FakeClient(pick(contract, ("30일 이내", "deadline")))
    rc.restore(contract, BASIS, client=client)
    first = [(item["id"], item["content"], item["version"]) for item in found(contract)]

    # 같은 모델·같은 문장이면 다시 묻지 않는다.
    rc.restore(contract, BASIS, client=client)
    assert len(client.prompts) == 1
    # 모델이 꺼져 있어도, 실패해도 남는다.
    rc.restore(contract, BASIS)
    rc.restore(contract, BASIS, client=FakeClient(error="연결 오류"))
    assert [(item["id"], item["content"], item["version"]) for item in found(contract)] == first

    # 문장이 달라지면 지난 답을 쓰지 않는다 — 번호가 다른 문장을 가리킬 수 있다.
    add(contract, "2025_용역_추가_안내.txt", "2025-09-01", "점검 결과는 별도 서식으로 보고한다.")
    rc.restore(contract, BASIS)
    assert found(contract) == []
    other = FakeClient(pick(contract, ("별도 서식", "howto")))
    rc.restore(contract, BASIS, client=other)
    assert [item["content"] for item in found(contract)] == ["점검 결과는 별도 서식으로 보고한다."]


def test_a_human_confirmation_survives_reanalysis(contract):
    client = FakeClient(pick(contract, ("30일 이내", "deadline")))
    rc.restore(contract, BASIS, client=client)
    item = found(contract)[0]
    contract.review_task_item(item["id"], "confirm")

    contract.con.execute("DELETE FROM meta WHERE key LIKE 'statements:%'")
    rc.restore(contract, BASIS, client=FakeClient({"found": []}))
    kept = found(contract)
    assert [row["id"] for row in kept] == [item["id"]]
    assert kept[0]["content"] == item["content"]          # 사람이 확인한 것은 지우지 않는다


def test_the_card_shows_found_sentences_apart_from_stated_values(contract):
    from PySide6.QtWidgets import QApplication, QLabel

    from app.ui import theme
    from app.ui.views.tasks import TasksView

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    rc.restore(contract, BASIS, client=FakeClient(pick(contract, ("기성금", "progress"))))
    view = TasksView(contract)
    view.open_task(1)
    shown = [label.text() for label in view.findChildren(QLabel)]
    assert "문장에서 찾은 내용" in shown
    assert workcard.FOUND_NOTE in shown
    assert any("1차 기성금을 지급하였고" in text for text in shown)
    view.close()
    view.deleteLater()
