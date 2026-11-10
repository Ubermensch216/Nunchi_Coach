"""업무 개요의 종합 요약 — 문장을 늘어놓지 않고 묶어서 보이고, 원문은 근거로 남긴다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import overview_summary as ov, work_brief
from app.core.task_manual import Section
from app.db import Database

TASK = {"id": 1, "name": "AI 기반 스마트 감사행정 지원시스템 구축",
        "description": "감사행정 지원시스템 구축 사업 관리"}
SENTENCES = [
    ("적용 조건", "대상시스템이 다른 네트워크 구간에 위치할 경우 방화벽 확인 필요"),
    ("작성·처리 요령", "대상시스템 서버환경과 네트워크 환경이 준비되면 출발지 서버의 SSH 인증키를 업로드한다."),
    ("작성·처리 요령", "감사 처리기간은 감사결과 공개까지의 실제 소요기간을 기준으로 산정한다."),
    ("적용 조건", "AI가 감사관의 판단을 대체하는 것이 아니라 감사 업무를 지원하는 시스템이다."),
    ("진행 상황", "정보자원 반입을 신청하오니 원활한 사업 추진을 위하여 협조하여 주시기 바랍니다."),
]


def found():
    return [{"kind": "statement", "slot": f"{cat} · {n:06x}", "content": text,
             "basis_state": "inferred", "applicability": "unknown", "human_state": "",
             "evidence": [{"filename": f"문서{n}.hwp"}]}
            for n, (cat, text) in enumerate(SENTENCES)]


class FakeClient:
    gen_model = "fake"

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def generate_json(self, prompt, schema, num_predict=400, temperature=0.0):
        self.calls += 1
        return self.answer, None, ""


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p" / "project.db")
    database.init()
    yield database
    database.close()


def test_rule_summary_is_short_and_names_keywords():
    result = ov.fallback(TASK, found())
    assert len(result["summary"]) + len(" · ".join(result["keywords"])) <= ov.LIMIT
    assert "감사" in result["keywords"] and "네트워크" in result["keywords"]
    assert "자료 문장 5건" in result["summary"]


def test_long_input_stays_within_limit():
    many = [{"slot": "적용 조건 · x", "content": "감사 시스템 네트워크 방화벽 " * 20 + f"{i}건이다."}
            for i in range(30)]
    result = ov.fallback(TASK, many)
    assert len(result["summary"]) + len(" · ".join(result["keywords"])) <= ov.LIMIT


def test_model_summary_is_cached_and_keywords_must_come_from_the_sources(db):
    client = FakeClient({"summary": "감사행정 지원시스템을 구축하는 사업이다. 서버·네트워크 연결을 준비한다.",
                         "keywords": ["감사", "네트워크", "블록체인"]})
    result = ov.refresh(db, TASK, found(), client)
    assert result["keywords"][:2] == ["감사", "네트워크"]      # 모델 키워드가 먼저
    assert "블록체인" not in result["keywords"]                # 자료에 없는 말은 버린다
    assert len(result["keywords"]) > 2                          # 모자라면 자료의 낱말로 채운다
    assert ov.refresh(db, TASK, list(reversed(found())), client) == result
    assert client.calls == 1                                    # 순서만 바뀐 같은 문장 묶음
    assert ov.read(db, TASK, found())["by"].startswith("fake:")
    assert ov.read(db, TASK, found()[:2])["by"] == "rule"       # 문장이 바뀌면 규칙 요약


def test_failed_model_answer_keeps_rule_summary(db):
    client = FakeClient({"found": []})
    assert ov.refresh(db, TASK, found(), client) is None
    assert ov.refresh(db, TASK, found(), client) is None
    assert client.calls == 1                                    # 같은 모델에 다시 묻지 않는다
    assert ov.read(db, TASK, found())["by"] == "rule"


def test_overview_shows_summary_first_and_sentences_as_evidence():
    keys = ("overview", "first", "timing", "output", "prepare", "procedure")
    sections = [Section(k, k) for k in keys]
    card = SimpleNamespace(todo=[], prepare=[], found=found(), stated_steps=[], precedent_steps=[],
                           actions=[], gaps=[], conflicting_procedure=None)
    work_brief.populate(TASK, card, sections, summary=ov.fallback(TASK, found()))
    brief = sections[0].brief
    assert brief.facts[0].label == "종합 요약" and brief.facts[1].label == "핵심 키워드"
    assert not any(f.label == "문장에서 찾은 내용" for f in brief.facts)
    assert brief.details_label == "근거 문장 5건"
    assert brief.details[0].startswith("[적용 조건] 대상시스템") and "문서0.hwp" in brief.details[0]
