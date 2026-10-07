"""문장 추출 정밀도 평가 도구 — 세는 법이 맞는지 본다.

모델을 부르지 않는다. 표본이 제품의 갈래와 어긋나지 않는지, 엉뚱하게 고른 문장과
놓친 문장을 정밀도·재현율로 옳게 세는지만 확인한다. 수치 자체는 실제 모델로
잰다(`python -m app.tools.statement_eval`).
"""

from __future__ import annotations

import json

from app.core import statements
from app.tools import statement_eval as se

GROUP = {"id": "g", "task": "t", "sentences": [
    {"text": "대금은 준공검사를 마친 뒤 30일 이내에 정산한다.", "accept": ["deadline"]},
    {"text": "실적표의 증감 사유는 전년 동기 대비로 적는다.", "accept": ["howto"]},
    {"text": "귀 기관의 무궁한 발전을 기원합니다.", "accept": []},
    {"text": "이상과 같이 보고합니다.", "accept": []},
]}


class Model:
    gen_model = "fake"

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.prompts = answer, error, []

    def generate_json(self, prompt, schema, num_predict=0):
        self.prompts.append(prompt)
        return (None, self.error, "") if self.error else (self.answer, None, "")


def test_the_sample_uses_only_the_product_categories_and_mixes_both_kinds():
    groups = json.loads(se.DEFAULT_CASES.read_text(encoding="utf-8"))["groups"]
    listed = [item for group in groups for item in group["sentences"]]
    assert all(set(item["accept"]) <= set(statements.CATEGORIES) for item in listed)
    useful = sum(1 for item in listed if item["accept"])
    assert useful >= 20 and len(listed) - useful >= 20      # 쓸모없는 문장이 넉넉히 섞여 있다
    assert all(len(group["sentences"]) <= statements.MAX_SENTENCES for group in groups)
    # 제품이 실제로 모델에 보내는 모양의 문장이어야 한다(이름표 없음, 서술어로 끝남).
    assert all(statements._split(item["text"]) for item in listed)


def test_wrong_picks_lower_precision_and_misses_lower_recall():
    tally = se.Tally()
    se.judge(GROUP, [[1, "deadline"], [3, "progress"]], tally)
    assert (tally.picked, tally.hits, tally.useful) == (2, 1, 2)
    assert tally.precision == 0.5 and tally.recall == 0.5
    assert tally.wrong == ["귀 기관의 무궁한 발전을 기원합니다.  → 진행 상황"]
    assert tally.missed == ["실적표의 증감 사유는 전년 동기 대비로 적는다."]


def test_a_useful_sentence_under_another_category_is_a_hit_but_not_the_right_kind():
    tally = se.Tally()
    se.judge(GROUP, [[1, "amount"], [2, "howto"]], tally)
    assert tally.precision == 1.0 and tally.recall == 1.0 and tally.kind_accuracy == 0.5


def test_the_model_is_asked_the_way_the_product_asks_and_bad_answers_are_dropped():
    model = Model({"found": [{"n": 1, "type": "deadline"}, {"n": 9, "type": "howto"},
                             {"n": 3, "type": "none"}, {"n": 2, "type": "기한"}]})
    tally = se.run(model, [GROUP])
    assert "1. 대금은 준공검사를" in model.prompts[0] and "4. 이상과 같이" in model.prompts[0]
    assert (tally.picked, tally.hits) == (1, 1)             # 없는 번호·없는 갈래·none은 버린다


def test_a_group_the_model_could_not_answer_counts_as_missed_not_as_clean():
    tally = se.run(Model(error="연결 오류"), [GROUP])
    assert tally.failed == 1 and tally.recall == 0.0 and len(tally.missed) == 2
    assert "답하지 못한 묶음 1" in se.to_text([tally], "fake")
