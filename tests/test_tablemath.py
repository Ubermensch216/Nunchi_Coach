"""표에서 가장 큰/작은 값을 규칙으로 답한다.

30문항 평가에서 모델은 완료율 92·88·95·90 가운데 90을 가장 높다고 했다. 값
견주기는 규칙으로 정할 수 있으므로 모델에 맡기지 않는다. 대신 조건이 하나라도
어긋나면 손대지 않는다 — 규칙이 틀린 답을 내면 모델보다 나을 것이 없다.
"""

from __future__ import annotations

from app.search.evidence import window
from app.search.tablemath import extreme

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
