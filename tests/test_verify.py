"""문장별 근거 검증 시험 (RAG 개선 R5 — 이 계획의 핵심).

모델이 "sources"를 스스로 밝혀도, 그 근거가 실제로 문장을 뒷받침하는지는
모델이 보장하지 않는다. 이 파일은 verify_sentences()가 그 격차를 규칙으로
메우는지 확인한다 — 근거 없는 문장, 지어낸 숫자, 존재하지 않는 인용
번호를 실제로 걸러내는가.
"""

from __future__ import annotations

from app.search.verify import verify_sentences

SOURCES = {
    1: "2025년 행정사무감사 요구자료 접수는 9월에 이루어졌다",
    2: "계약관리 업무의 처리건수는 1분기 41건이었다",
}


def sentence(text: str, sources: list) -> dict:
    return {"text": text, "sources": sources}


# ── 근거 없는 문장 ──────────────────────────────────────────────────

def test_sentence_without_sources_is_dropped():
    result = verify_sentences([sentence("근거 없이 말한다", [])], SOURCES)
    assert result.kept == []
    assert result.dropped[0].reason == "근거를 스스로 밝히지 않았다"


def test_sentence_with_null_sources_is_dropped():
    """모델이 sources를 아예 안 주면 None일 수 있다 — []와 같이 취급한다."""
    result = verify_sentences([{"text": "말한다", "sources": None}], SOURCES)
    assert result.kept == []


def test_blank_text_is_silently_skipped_not_reported_as_dropped():
    """빈 문장은 검증 실패가 아니라 애초에 문장이 아니다 — 사유를 남기지 않는다."""
    result = verify_sentences([sentence("   ", [1])], SOURCES)
    assert result.kept == []
    assert result.dropped == []


# ── 존재하지 않는 근거 번호 ─────────────────────────────────────────

def test_sentence_citing_a_nonexistent_source_is_dropped():
    result = verify_sentences([sentence("9월에 접수되었다", [99])], SOURCES)
    assert result.kept == []
    assert result.dropped[0].reason == "존재하지 않는 근거 번호를 인용했다"


def test_partially_valid_sources_keep_only_the_real_ones():
    """일부는 진짜 번호, 일부는 지어낸 번호 — 진짜 것만으로 검증한다."""
    result = verify_sentences([sentence("9월에 접수되었다", [1, 99])], SOURCES)
    assert result.kept and result.kept[0].sources == [1]


# ── 숫자 검증 ───────────────────────────────────────────────────────

def test_number_absent_from_cited_source_is_dropped():
    """실측: '41건/38건/45건/39건'처럼 무관한 표를 그대로 옮겨 붙인 사례가 있었다."""
    result = verify_sentences(
        [sentence("2분기에는 88건이 처리되었다", [1])], SOURCES,
    )
    assert result.kept == []
    assert "88" in result.dropped[0].reason


def test_number_present_in_cited_source_survives():
    result = verify_sentences(
        [sentence("1분기 처리건수는 41건이었다", [2])], SOURCES,
    )
    assert len(result.kept) == 1


def test_single_digit_numbers_are_not_flagged():
    """'1분기'의 '1'처럼 한 자리 순번까지 걸면 정상 문장을 과하게 지운다."""
    result = verify_sentences(
        [sentence("1분기 처리건수를 확인했다", [2])], SOURCES,
    )
    assert len(result.kept) == 1


def test_trailing_punctuation_is_not_treated_as_part_of_the_number():
    """실측으로 잡은 결함: '820, 2분기 0'에서 '820,'을 통째로 숫자로 잡았다.

    쉼표·마침표를 숫자 문자로 받는 것은 '1,234'·'3.14' 때문인데, 그대로 두면
    숫자 뒤에 쉼표가 오는 정상 문장이 전부 버려진다.
    """
    sources = {1: "예산요구액 820 집행액 210"}
    result = verify_sentences(
        [sentence("예산요구액은 820, 집행액은 210.", [1])], sources,
    )
    assert len(result.kept) == 1, result.dropped and result.dropped[0].reason


def test_thousands_separator_inside_a_number_is_preserved():
    """'1,234'는 숫자 하나다 — 쉼표를 무조건 떼면 이쪽이 깨진다."""
    result = verify_sentences(
        [sentence("금액은 1,234원이다", [1])], {1: "총액 5,678원"},
    )
    assert result.kept == []
    assert "1,234" in result.dropped[0].reason


def test_number_check_looks_only_at_the_sentences_own_cited_sources():
    """근거로 밝히지 않은 다른 조각에 그 숫자가 있어도 소용없다 — 인용을 안 했다."""
    result = verify_sentences(
        [sentence("1분기 처리건수는 41건이었다", [1])], SOURCES,   # source 1에는 '41'이 없다
    )
    assert result.kept == []


# ── 어휘 중첩 ───────────────────────────────────────────────────────

def test_sentence_unrelated_to_its_cited_source_is_dropped():
    result = verify_sentences(
        [sentence("오늘 날씨가 참 좋고 기분이 상쾌하다", [1])], SOURCES,
    )
    assert result.kept == []
    assert result.dropped[0].reason == "인용한 근거와 겹치는 말이 거의 없다"


def test_sentence_grounded_in_its_source_survives():
    result = verify_sentences(
        [sentence("행정사무감사 요구자료 접수는 9월에 이루어졌다", [1])], SOURCES,
    )
    assert len(result.kept) == 1


# ── 여러 문장 ───────────────────────────────────────────────────────

def test_some_sentences_survive_while_others_are_dropped():
    """전부 버리지 않는다 — 나쁜 문장 하나가 좋은 문장까지 끌고 내려가지 않는다."""
    result = verify_sentences([
        sentence("행정사무감사 요구자료 접수는 9월에 이루어졌다", [1]),
        sentence("근거 없이 지어낸 말이다", []),
    ], SOURCES)
    assert len(result.kept) == 1
    assert len(result.dropped) == 1


def test_multi_source_sentence_needs_the_number_in_any_cited_source():
    """여러 근거를 합친 문장은 숫자가 그중 하나에만 있어도 통과한다."""
    result = verify_sentences(
        [sentence("접수는 9월이었고 처리건수는 41건이었다", [1, 2])], SOURCES,
    )
    assert len(result.kept) == 1


def test_empty_input_yields_empty_result():
    result = verify_sentences([], SOURCES)
    assert result.kept == []
    assert result.dropped == []
    assert not result.survived


def test_non_dict_items_are_ignored_without_crashing():
    """모델이 스키마를 어겨 문자열 등을 섞어 보내도 죽지 않는다."""
    result = verify_sentences(["문자열", 42, None, sentence("행정사무감사 접수", [1])], SOURCES)
    assert len(result.kept) == 1


def test_boolean_is_not_mistaken_for_a_source_index():
    """bool은 파이썬에서 int의 서브클래스다 — 실수로 근거 번호로 세면 안 된다."""
    result = verify_sentences([sentence("접수되었다", [True, False])], SOURCES)
    assert result.kept == []
    assert result.dropped[0].reason == "근거를 스스로 밝히지 않았다"


def test_survived_reflects_whether_anything_is_kept():
    assert verify_sentences([sentence("행정사무감사 접수", [1])], SOURCES).survived
    assert not verify_sentences([sentence("근거 없음", [])], SOURCES).survived


# ── 표의 칸 대조 ────────────────────────────────────────────────────
# 30문항 평가에서 "예산 집행액이 2분기에 적었던 이유가 뭐야?"에 "2분기 집행액은
# 430입니다"가 나갔다. 430은 3분기 칸이고 2분기는 210이다. 숫자가 근거에 있다는
# 것만으로는 표를 옳게 읽었다고 할 수 없다.

from app.search.evidence import window  # noqa: E402


def shown(text: str) -> str:
    """검증이 실제로 보는 글 — 증거창을 거친 한 줄."""
    return window(text, "")


SHEET = {1: shown("[집계]\n구분\t1분기\t2분기\t3분기\t4분기\n"
                  "예산요구액\t820\t0\t0\t0\n집행액\t0\t210\t430\t180")}


def kept(text: str, sources=SHEET) -> bool:
    return bool(verify_sentences([sentence(text, [1])], sources).kept)


def test_a_value_from_another_cell_of_the_same_table_is_dropped():
    result = verify_sentences([sentence("2분기 집행액은 430입니다.", [1])], SHEET)
    assert result.kept == []
    assert result.dropped[0].reason == "표에서 짚은 칸의 값이 아니다: 430"
    assert not kept("예산요구액은 3분기에 820이다.")


def test_the_value_of_the_named_cell_passes():
    assert kept("2분기 집행액은 210입니다.")
    assert kept("집행액은 3분기에 430이었다.")
    # 띄어 쓴 행 이름도 같은 행이다.
    assert kept("예산 요구액은 1분기 820, 2분기 0, 3분기 0, 4분기 0이다.")
    # 여러 칸을 함께 말하면 그 칸들의 값을 모두 허용한다.
    assert kept("집행액은 2분기 210, 3분기 430이다.")


def test_sentences_that_do_not_name_both_a_row_and_a_column_are_left_alone():
    """어느 칸을 말하는지 정할 수 없으면 판정하지 않는다."""
    assert kept("집행액은 430까지 늘었다.")                 # 열을 말하지 않았다
    assert kept("3분기에는 430이 집계되었다.")              # 행을 말하지 않았다
    table = {1: shown("항목\t수량\t비고\n요구자료\t12\t제출완료\n추가질의\t3\t답변완료")}
    assert kept("요구자료는 12건이며 제출완료되었다.", table)


def test_row_form_tables_are_checked_the_same_way():
    row = {1: shown("첫 행: 사업 | 예산 | 집행액\n1열: 시범사업 | 2열: 820 | 3열: 610\n단위: 백만원")}
    assert row[1] == "첫 행: 사업 | 예산 | 집행액 1열: 시범사업 | 2열: 820 | 3열: 610 / 단위: 백만원"
    assert kept("시범사업의 예산은 820이다.", row)
    assert not kept("시범사업의 예산은 610이다.", row)


def test_a_number_in_a_row_or_column_name_is_not_taken_for_a_value():
    quarters = {1: shown("구분\t1분기\t2분기\n처리건수\t2\t1")}
    assert kept("2분기 처리건수는 1건이다.", quarters)
    assert not kept("2분기 처리건수는 2건이다.", quarters)


def test_tables_from_different_evidence_pieces_are_not_joined():
    """근거 둘의 표를 하나로 이으면 열이 어긋나 멀쩡한 답을 버린다."""
    sources = {1: SHEET[1], 2: shown("구분\t목표\t실적\n집행액\t500\t430")}
    result = verify_sentences([sentence("집행액은 3분기에 430이고 목표는 500이다.", [1, 2])], sources)
    assert result.kept, result.dropped


def test_the_evidence_window_keeps_cell_and_row_boundaries():
    assert SHEET[1] == ("[집계] 구분 | 1분기 | 2분기 | 3분기 | 4분기 / "
                        "예산요구액 | 820 | 0 | 0 | 0 / 집행액 | 0 | 210 | 430 | 180 /")


# ── 금액 단위 ───────────────────────────────────────────────────────

def test_a_money_unit_that_the_evidence_does_not_carry_is_dropped():
    """단위 없는 표의 210에 다른 문서의 '백만원'을 붙인 답이 나갔다."""
    result = verify_sentences([sentence("2분기 집행액은 210백만원입니다.", [1])], SHEET)
    assert result.kept == []
    assert result.dropped[0].reason == "인용한 근거에 없는 금액 단위를 썼다: 백만원"
    with_unit = {1: shown("첫 행: 사업 | 예산\n1열: 시범사업 | 2열: 820\n단위: 백만원")}
    assert kept("시범사업의 예산은 820백만원이다.", with_unit)
    assert kept("2분기 집행액은 210입니다.")
    assert kept("계약금액은 48,000천원이다.", {1: "계약금액: 48,000천원"})


# ── 대신 답하기 ─────────────────────────────────────────────────────

def test_saying_there_is_no_answer_and_offering_something_else_is_dropped():
    sources = {1: "2025년 행정사무감사 요구자료 접수 문서와 제출자료 문서가 작성되었다"}
    hedge = "요구자료의 수량에 대한 구체적인 정보는 없으나, 요구자료 접수 및 제출자료 관련 문서들이 존재합니다."
    result = verify_sentences([sentence(hedge, [1])], sources)
    assert result.kept == [] and result.dropped[0].reason == "답이 없다고 하면서 다른 내용을 내놓았다"
    # 자료가 그렇게 적고 있는 것을 옮긴 문장은 그대로 둔다.
    plain = {1: "요구자료 접수 이후 추가 요청은 없었다"}
    assert verify_sentences([sentence("요구자료 접수 이후 추가 요청은 없었다.", [1])], plain).kept
