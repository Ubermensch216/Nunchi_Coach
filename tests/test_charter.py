"""업무분장표·업무기술서 탐지 및 추출 단위 시험.

- 후보 파일 판정 (파일명, 폴더명, 확장자)
- 합성 분장표 3종 추출 (엑셀 표, HWP 표, 개조식 HWPX)
- 과 전체 분장표에서 전임자 행만 고름 (타 담당자 0건)
- DB 저장 및 계층 구조 확인
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import charter
from app.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    db_file = tmp_path / "project.db"
    database = Database(db_file)
    database.init()
    return database


def test_is_charter_candidate():
    assert charter.is_charter_candidate("2024년_업무분장표.xlsx")
    assert charter.is_charter_candidate("사무분담현황.hwpx")
    assert charter.is_charter_candidate("직무기술서_최종.hwp")
    assert charter.is_charter_candidate("인수인계서(홍길동).pdf")
    assert charter.is_charter_candidate("담당업무.docx")
    assert charter.is_charter_candidate("파일.xlsx", path="C:/data/업무분장/파일.xlsx")

    # 대상 아님
    assert not charter.is_charter_candidate("일반보고서.hwp")
    assert not charter.is_charter_candidate("업무분장.txt")  # 지원 확장자 외
    assert not charter.is_charter_candidate("업무분장.png")


def test_extract_excel_table():
    """엑셀 표 형태 (탭 구분자)."""
    text = (
        "연번\t직급\t성명\t담당업무\t비중\n"
        "1\t행정6급\t김철수\t- 일반서무 및 보안관리\n- 청사 시설 관리\t40%\n"
        "2\t행정7급\t홍길동\t1. 예산 편성 및 집행 관리\n2. 주요 사업 추진계획 수립\t60%\n"
        "3\t행정8급\t이영희\t- 민원 접수 및 처리\n- 회계 지출\t50%\n"
    )

    # 전임자 '홍길동'만 추출
    items = charter.extract_items(text, predecessor="홍길동")
    assert len(items) == 2
    names = [it.name for it in items]
    assert any("예산 편성" in n for n in names)
    assert any("주요 사업" in n for n in names)
    for it in items:
        assert it.owner == "홍길동"

    # 타 담당자 업무는 포함되지 않아야 함
    assert not any("서무" in n for n in names)
    assert not any("민원" in n for n in names)


def test_extract_hwp_table():
    """HWP 표 형태 (다중 공백 또는 파이프 구분)."""
    text = (
        "| 직위 | 담당자 | 주요업무 |\n"
        "| 팀장 | 박팀장 | 팀 총괄 및 주요 정책 결정 |\n"
        "| 주무관 | 홍길동 | - 규제개혁 추진계획 수립\n  * 세부실천과제 발굴\n- 규제입증책임제 운영 |\n"
        "| 주무관 | 최민수 | 정보화 사업 추진 |\n"
    )

    items = charter.extract_items(text, predecessor="홍길동")
    assert len(items) >= 2
    names = [it.name for it in items]
    assert any("규제개혁" in n for n in names)
    assert any("규제입증책임제" in n for n in names)

    # 세부 업무 확인
    reg_item = next(it for it in items if "규제개혁" in it.name)
    assert any("세부실천과제" in d for d in reg_item.details)


def test_extract_outline_hwpx():
    """개조식 HWPX 형태 (문단 불릿 및 담당자 표시)."""
    text = (
        "2024년도 부서 사무분장\n\n"
        "[홍길동 주무관]\n"
        "1. 신규 사업 기획 (40%)\n"
        "   - 중장기 발전계획 수립\n"
        "   - 타당성 조사 용역 추진\n"
        "2. 대외협력 및 홍보 (30%)\n"
        "   - 언론 보도자료 배포\n"
        "3. 부서 예산 관리 (30%)\n\n"
        "[김영수 주무관]\n"
        "1. 청사 방역 및 환경관리\n"
        "2. 물품 수급 관리\n"
    )

    items = charter.extract_items(text, predecessor="홍길동")
    assert len(items) == 3
    names = [it.name for it in items]
    assert any("신규 사업 기획" in n for n in names)
    assert any("대외협력" in n for n in names)
    assert any("예산 관리" in n for n in names)

    # 김영수 주무관 업무는 포함되지 않음
    assert not any("방역" in n for n in names)

    plan_item = next(it for it in items if "신규 사업 기획" in it.name)
    assert plan_item.weight == 40.0
    assert len(plan_item.details) == 2


def test_save_and_get_charter_items(db: Database, tmp_path: Path):
    src_id = db.add_source(tmp_path)
    f = tmp_path / "업무분장.xlsx"
    f.write_text("dummy")
    doc_id = db.upsert_document(src_id, {
        "path": str(f), "filename": f.name, "ext": ".xlsx",
        "hash": "hash_charter", "parse_status": "ok",
    })

    items = [
        charter.ExtractedItem(
            name="예산편성",
            details=["본예산 편성", "추경예산 편성"],
            owner="홍길동",
            weight=50.0,
            locator="row:2",
        ),
        charter.ExtractedItem(
            name="성과관리",
            details=[],
            owner="홍길동",
            weight=50.0,
            locator="row:3",
        ),
    ]

    saved_ids = charter.save_charter_items(db, doc_id, items)
    # 2개 상위 + 2개 세부 = 4개
    assert len(saved_ids) == 4

    rows = charter.get_charter_items(db, doc_id)
    assert len(rows) == 4

    top_items = [r for r in rows if r["parent_id"] is None]
    sub_items = [r for r in rows if r["parent_id"] is not None]
    assert len(top_items) == 2
    assert len(sub_items) == 2
    assert top_items[0]["name"] == "예산편성"


_DEPARTMENT_TABLE = (
    "연번\t직급\t성명\t담당업무\t비중\n"
    "1\t행정6급\t김철수\t- 일반서무 및 보안관리\t40%\n"
    "2\t행정7급\t홍길동\t1. 예산 편성 및 집행 관리\t60%\n"
)


def _charter_document(db: Database, tmp_path: Path) -> int:
    source = db.add_source(tmp_path)
    doc_id = db.upsert_document(source, {
        "path": str(tmp_path / "2024년_업무분장표.xlsx"), "filename": "2024년_업무분장표.xlsx",
        "ext": ".xlsx", "parse_status": "ok", "hash": "charter"})
    db.replace_sections(doc_id, [("table", 1, "표1", _DEPARTMENT_TABLE, {})])
    return doc_id


def test_predecessor_comes_from_the_work_context_when_not_typed(db: Database):
    assert charter.predecessor_name(db) == ""
    db.start_work_context("with_predecessor", predecessor="홍길동")
    assert charter.predecessor_name(db) == "홍길동"
    charter.set_predecessor(db, "  김 철수 ")
    assert charter.predecessor_name(db) == "김 철수"


def test_typing_the_predecessor_later_reextracts_only_their_rows(db: Database, tmp_path: Path):
    """파싱 때는 이름을 몰라 과 전체를 뽑았어도, 이름을 적고 다시 분석하면 그 사람 행만 남는다."""
    doc_id = _charter_document(db, tmp_path)
    charter.extract_from_document(db, doc_id)
    assert {item["owner"] for item in charter.get_charter_items(db)} == {"김철수", "홍길동"}
    assert charter.refresh_items(db) == 0          # 이름이 그대로면 다시 뽑지 않는다

    charter.set_predecessor(db, "홍길동")
    assert charter.refresh_items(db) == 1
    items = charter.get_charter_items(db)
    assert items and {item["owner"] for item in items} == {"홍길동"}
    assert not any("서무" in item["name"] for item in items)
    assert charter.refresh_items(db) == 0


def test_user_decided_items_survive_reextraction(db: Database, tmp_path: Path):
    doc_id = _charter_document(db, tmp_path)
    charter.save_charter_items(db, doc_id, [charter.ExtractedItem(name="사람이 넣은 업무")],
                               decided_by="user")
    charter.set_predecessor(db, "홍길동")
    charter.refresh_items(db)
    assert "사람이 넣은 업무" in {item["name"] for item in charter.get_charter_items(db)}
