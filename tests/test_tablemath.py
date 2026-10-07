"""표에서 가장 큰/작은 값을 규칙으로 답한다.

30문항 평가에서 모델은 완료율 92·88·95·90 가운데 90을 가장 높다고 했다. 값
견주기는 규칙으로 정할 수 있으므로 모델에 맡기지 않는다. 대신 조건이 하나라도
어긋나면 손대지 않는다 — 규칙이 틀린 답을 내면 모델보다 나을 것이 없다.
"""

from __future__ import annotations

from app.search.evidence import window
from app.search.tablemath import count, extreme

TALLY = window("[집계]\n구분\t1분기\t2분기\t3분기\t4분기\n"
               "처리건수\t41\t38\t45\t39\n완료율\t92\t88\t95\t90", "")
BODY = "2025년 월간실적보고 실적 취합. 담당부서는 관련 자료를 취합하여 제출하였다."


def test_highest_and_lowest_of_the_named_row():
    text, sources = extreme("월간실적보고 완료율이 가장 높았던 분기의 완료율은 얼마야?",
                            {1: BODY, 2: TALLY})
    assert "3분기의 95이(가) 가장 큽니다" in text and "1분기 92, 2분기 88" in text
    assert sources == [2]
    text, _ = extreme("처리건수가 가장 적은 분기는 언제야?", {1: TALLY})
    assert "2분기의 38이(가) 가장 작습니다" in text
    assert extreme("완료율 최고치는?", {1: TALLY})[0].count("95") == 2


def test_the_same_table_in_several_pieces_is_one_answer_with_a_few_sources():
    text, sources = extreme("완료율이 가장 높은 분기는?", {n: TALLY for n in range(1, 9)})
    assert "95" in text and sources == [1, 2, 3]      # 근거를 전부 늘어놓지 않는다


def test_it_stays_out_when_the_question_or_the_table_is_not_clear_cut():
    ask = "완료율이 가장 높은 분기는?"
    assert extreme("완료율은 3분기에 얼마야?", {1: TALLY}) is None            # 견주는 질문이 아니다
    assert extreme("가장 높은 분기는 언제야?", {1: TALLY}) is None            # 어느 행인지 모른다
    assert extreme("처리건수와 완료율이 가장 높은 분기는?", {1: TALLY}) is None   # 행이 둘이다
    assert extreme("가장 높고 가장 낮은 완료율은?", {1: TALLY}) is None       # 방향이 둘이다
    assert extreme(ask, {1: BODY}) is None                                    # 표가 없다
    other = window("구분\t1분기\t2분기\n완료율\t70\t99", "")
    assert extreme(ask, {1: TALLY, 2: other}) is None                         # 근거마다 값이 다르다
    tie = window("구분\t1분기\t2분기\t3분기\n완료율\t95\t88\t95", "")
    assert extreme(ask, {1: tie}) is None                                     # 가장 큰 값이 둘이다
    words = window("구분\t1분기\t2분기\n완료율\t높음\t95", "")
    assert extreme(ask, {1: words}) is None                                   # 숫자가 아닌 칸이 있다


# ── 표의 한 칸에 적힌 수량 ──────────────────────────────────────────

AUDIT = window("항목\t수량\t비고\n요구자료\t12\t제출완료\n추가질의\t3\t답변완료", "")
AUDIT_BODY = "2025년 행정사무감사 요구자료 접수. 담당부서는 관련 자료를 취합하여 제출하였다."


def test_a_count_written_in_one_table_cell_is_read_by_rule():
    text, sources = count("행정사무감사 요구자료는 몇 건이었어?", {1: AUDIT_BODY, 2: AUDIT},
                          {1: AUDIT_BODY, 2: AUDIT_BODY + "\n" + AUDIT})
    assert text == "표의 '요구자료' 행 '수량' 칸 값은 12입니다." and sources == [2]
    # 열 이름을 불렀으면 그 칸이다.
    text, _ = count("1분기 처리건수는 몇 건이야?", {1: TALLY})
    assert "'처리건수' 행 '1분기' 칸 값은 41" in text
    # 같은 표가 여러 근거에 있어도 답은 하나, 근거는 몇 건만.
    assert count("요구자료 수량은?", {n: AUDIT for n in range(1, 9)})[1] == [1, 2, 3]


def test_the_count_rule_stays_out_when_the_cell_is_not_certain():
    assert count("요구자료는 어떻게 처리됐어?", {1: AUDIT}) is None            # 수량을 묻지 않았다
    assert count("요구자료는 몇 월에 접수했어?", {1: AUDIT}) is None           # '몇 월'은 수량이 아니다
    assert count("요구자료와 추가질의는 몇 건이야?", {1: AUDIT}) is None       # 행이 둘이다
    assert count("감사 지적사항은 몇 건이야?", {1: AUDIT}) is None             # 그런 행이 없다
    assert count("처리건수는 몇 건이야?", {1: TALLY}) is None                  # 어느 분기인지 모른다
    assert count("처리건수가 가장 많은 분기는 몇 건이야?", {1: TALLY}) is None  # 견주는 질문이다
    # 표가 말하지 않는 것을 물었다 — '목표치'·'어떻게'는 문서에 없다.
    assert count("1분기 처리건수 목표치는 몇 건이야?", {1: TALLY}) is None
    assert count("추가질의는 몇 건이고 어떻게 처리됐어?", {1: AUDIT}) is None
    other = window("항목\t수량\t비고\n요구자료\t15\t제출완료", "")
    assert count("요구자료는 몇 건이야?", {1: AUDIT, 2: other}) is None        # 근거마다 값이 다르다
    words = window("항목\t수량\t비고\n요구자료\t다수\t제출완료", "")
    assert count("요구자료는 몇 건이야?", {1: words}) is None                  # 숫자가 아니다
    price = window("항목\t단가\t비고\n요구자료\t12\t제출완료", "")
    assert count("요구자료는 몇 건이야?", {1: price}) is None                  # 수량 열이 아니다


def test_only_the_documents_that_carry_every_word_of_the_question_are_used():
    this_year = "2025년 행정사무감사 요구자료 접수"
    last_year = "2024년 행정사무감사 요구자료 접수"
    other = window("항목\t수량\t비고\n요구자료\t15\t제출완료", "")
    text, sources = count("2025년 행정사무감사 요구자료는 몇 건이었어?", {1: AUDIT, 2: other},
                          {1: this_year + "\n" + AUDIT, 2: last_year + "\n" + other})
    assert "12" in text and sources == [1]


def test_a_cell_named_by_row_and_column_is_read_even_when_asked_how_much():
    """60문항 평가: 모델은 옆 행의 값을 답했고 검증이 버려 유보로 끝났다."""
    budget = window("[집계]\n구분\t1분기\t2분기\t3분기\t4분기\n"
                    "예산요구액\t820\t0\t0\t0\n집행액\t0\t210\t430\t180", "")
    about = {1: "2025_예산관리_예산요구자료.xlsx\n" + budget}
    text, _ = count("예산관리 집계표에서 1분기 예산요구액은 얼마야?", {1: budget}, about)
    assert "'예산요구액' 행 '1분기' 칸 값은 820" in text
    assert "'완료율' 행 '4분기' 칸 값은 90" in count("4분기 완료율은 얼마야?", {1: TALLY})[0]
    # '얼마'는 행과 열을 모두 불렀을 때만이다.
    assert count("요구자료는 얼마야?", {1: AUDIT}) is None
    assert count("집행액은 얼마야?", {1: budget}, about) is None
    # 문서에 없는 것을 물었다.
    assert count("시범사업의 4분기 집행액은 얼마야?", {1: budget}, about) is None
