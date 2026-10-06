"""증거창 — 조각 뒤쪽의 금액·기한이 답변 재료와 검증에 함께 남는가 (B05)."""

from pathlib import Path

import pytest

from app.db import Database
from app.search import evidence
from app.search.rag import _context_budget, ask
from app.search.vector import pack
from tests.test_rag import FakeClient

FILLER = "이 문단은 사업 추진 배경과 일반 현황을 설명한다. " * 20
LONG = FILLER + "정기 실적 보고의 제출기한은 2025년 9월 30일이다. 총 요구액은 820백만원이다."


def test_short_chunks_are_passed_through_unchanged():
    assert evidence.window("본문  내용\n둘째 줄", "아무 질문") == "본문 내용 둘째 줄"


def test_value_at_the_end_of_a_long_chunk_survives():
    assert "9월 30일" not in " ".join(LONG.split())[:400]
    window = evidence.window(LONG, "제출기한이 언제야?", 400)
    assert len(window) <= 400
    assert "제출기한은 2025년 9월 30일이다." in window


def test_skipped_text_is_marked_and_original_order_is_kept():
    text = ("계약 체결일은 3월 2일이다. " + FILLER + "계약 금액은 1,200천원이다. "
            + FILLER + "계약 종료일은 12월 31일이다.")
    window = evidence.window(text, "계약 금액과 종료일", 300)
    assert window.index("체결일") < window.index("1,200천원") < window.index("종료일은")
    assert "…" in window and len(window) <= 300


def test_table_context_lines_always_stay_with_the_row():
    row = ("첫 행: 사업 | 요구액\n" + " | ".join(f"{i}열: 항목{i}" for i in range(1, 80))
           + " | 80열: 시범사업 820\n단위: 백만원")
    window = evidence.window(row, "시범사업 요구액", 300)
    assert window.startswith("첫 행: 사업 | 요구액 단위: 백만원")
    assert "시범사업 820" in window and len(window) <= 300


def test_falls_back_to_the_head_when_nothing_matches():
    assert evidence.window(LONG, "전혀 다른 주제", 400) == " ".join(LONG.split())[:400]


def test_dates_are_not_cut_at_their_periods():
    text = FILLER + "작성일은 2024. 9. 12. 이며 담당 부서가 확인했다."
    assert "작성일은 2024. 9. 12. 이며" in evidence.window(text, "작성일", 300)


def test_budget_grows_when_evidence_is_scarce_and_never_exceeds_the_old_worst_case():
    assert _context_budget(1) == 1200
    assert _context_budget(8) == 600
    assert _context_budget(24) == 400


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


def test_generation_verification_and_snippet_share_the_same_window(db):
    """뒤쪽 기한을 옳게 답한 문장이 '근거에 없는 숫자'로 버려지지 않는다."""
    source = db.add_source(r"D:\자료")
    for index in range(13):   # 근거가 많아 조각당 예산이 400자로 줄어드는 상황
        doc = db.upsert_document(source, {
            "path": rf"D:\자료\실적보고{index}.hwpx", "filename": f"실적보고{index}.hwpx",
            "ext": ".hwpx", "parse_status": "ok", "hash": f"h{index}"})
        text = LONG if index == 0 else f"실적 보고 관련 참고 자료 {index}번이다."
        chunk_id, _ = db.replace_document_chunks(doc, [(1, "1~9문단", text)])[0]
        vector = [1.0, 0.0, 0.0] if index == 0 else [0.9, 0.1 + index / 100, 0.0]
        db.save_chunk_embedding(chunk_id, "bge-m3", 3, pack(vector))

    class Recording(FakeClient):
        def generate_json(self, prompt, schema, num_predict=400, temperature=0.0):
            self.prompt = prompt
            return super().generate_json(prompt, schema, num_predict, temperature)

    client = Recording(gen_response={"answered": True, "sentences": [
        {"text": "정기 실적 보고의 제출기한은 2025년 9월 30일이다.", "sources": [1]},
        {"text": "총 요구액은 999백만원이다.", "sources": [1]},
    ]})
    answer = ask(db, "실적 보고 제출기한이 언제야?", client=client)

    assert "제출기한은 2025년 9월 30일이다." in client.prompt
    assert not answer.withheld
    assert "9월 30일" in answer.text and "999" not in answer.text
    assert "9월 30일" in answer.citations[0].snippet
