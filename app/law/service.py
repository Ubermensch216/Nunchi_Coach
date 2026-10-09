"""법령 조회를 대화에 잇는다 — 켜짐 판정, 조문 모으기, 조문만으로 답하기.

답은 조회한 조문 본문만 근거로 만든다. 모델이 아는 법 지식으로 채우지 않는다 —
경량 모델은 조문 번호와 내용을 그럴듯하게 지어낸다. 그래서 문장마다 조문
번호를 달게 하고 자료 답변과 같은 규칙 검증(search/verify.py)을 거친다.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from ..ai.prompts_loader import render
from ..ai.schemas import ASK_SCHEMA
from ..db.repo import default_project_dir
from ..search.verify import verify_sentences
from . import refs
from .client import LawArticle, LawClient, LawError

SETTINGS_ENV = "NUNCHICOACH_USER_SETTINGS"
MAX_ARTICLES = 4            # 한 번의 질문에 조회하는 조문 수
MAX_FOUND_BY_SEARCH = 3     # 조문 인용이 없을 때 검색으로 찾아 읽는 조문 수
ARTICLE_CONTEXT_CHARS = 1800
ANSWER_TOKENS = 700


# ── 설정 ────────────────────────────────────────────────────────────
def settings_path() -> Path:
    """인증키를 두는 자리. 프로젝트 DB가 아니라 이 PC 사용자의 폴더다.

    프로젝트 폴더는 USB·공유 폴더로 옮겨 다니고 묶음으로 내보내기도 한다.
    개인에게 발급된 키가 거기 실려 다니면 안 된다.
    """
    override = os.environ.get(SETTINGS_ENV)
    return Path(override) if override else default_project_dir().parent / "settings.json"


def _read() -> dict:
    try:
        value = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def api_key() -> str:
    return str(_read().get("law_oc") or "").strip()


def save_api_key(value: str) -> None:
    value = value.strip()
    if any(c.isspace() for c in value):
        raise ValueError("인증키에 공백이 들어 있습니다. 발급받은 값을 그대로 입력하세요.")
    data = _read()
    if value:
        data["law_oc"] = value
    else:
        data.pop("law_oc", None)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def enabled(db) -> bool:
    return db.get_meta("law_enabled") == "1"


def set_enabled(db, on: bool) -> None:
    db.set_meta("law_enabled", "1" if on else "0")
    db.audit("law.setting", result="enabled" if on else "disabled")


def available(db) -> bool:
    """이 인수인계에서 지금 법령을 조회할 수 있는가."""
    return enabled(db) and bool(api_key())


def check(oc: str, transport=None) -> str:
    """연결 확인. 누구나 아는 법령 하나를 찾아 본다 — 사용자 자료와 무관한 질의다."""
    hits = LawClient(oc, transport=transport).search_law("민법", display=1)
    if not hits:
        raise LawError("연결은 됐지만 검색 결과를 받지 못했습니다. 인증키(OC)를 확인하세요.", "upstream")
    return f"연결됨 · 시험 조회 「{hits[0].law_name}」"


# ── 조문 모으기 ─────────────────────────────────────────────────────
def gather(client: LawClient, question: str, cited_in_documents: list[refs.LawRef] | None = None
           ) -> tuple[list[tuple[LawArticle, str]], list[str]]:
    """질문에 답할 조문을 모은다. ((조문, 어디서 나온 인용인가) 목록, 알릴 문제).

    ① 질문에 적힌 조문 → ② 자료가 인용한 조문 → ③ 둘 다 없으면 질문 문장으로 검색.
    ③만 질문 문장을 그대로 보낸다. ①②는 법령 이름과 조문 번호만 보낸다.
    """
    wanted: list[tuple[refs.LawRef, str]] = [(ref, "question") for ref in refs.extract(question)]
    wanted += [(ref, "document") for ref in (cited_in_documents or [])
               if ref not in [w[0] for w in wanted]]
    problems: list[str] = []
    if not wanted:
        try:
            wanted = [(ref, "search") for ref in client.search_articles(question)[:MAX_FOUND_BY_SEARCH]]
        except LawError as exc:
            problems.append(str(exc))
    articles: list[tuple[LawArticle, str]] = []
    for ref, origin in wanted[:MAX_ARTICLES]:
        try:
            article = client.article(ref)
        except LawError as exc:
            problems.append(str(exc))
            if exc.kind in ("network", "not_configured"):
                break            # 연결이 안 되는데 계속 두드릴 이유가 없다
            continue
        if not any(a.law_name == article.law_name and a.article == article.article for a, _ in articles):
            articles.append((article, origin))
    if not articles and not problems:
        named = refs.names(question)
        problems.append(
            f"‘{named[0]}’의 어느 조문인지 알 수 없어 본문을 조회하지 못했습니다. 조문 번호를 함께 적어 보세요."
            if named else "질문과 관련된 조문을 찾지 못했습니다. 법령 이름과 조문 번호를 적어 보세요.")
    return articles, problems


def _citation(index: int, article: LawArticle, origin: str) -> dict:
    return {"index": index, "origin": origin, **asdict(article)}


def answer(db, question: str, llm=None, law_client: LawClient | None = None,
           cited_in_documents: list[refs.LawRef] | None = None) -> dict:
    """현행 조문을 조회해 그 조문만으로 답한다. 예외를 던지지 않는다.

    돌려주는 값은 대화 한 줄에 그대로 저장된다:
      {"text": 답 또는 "", "citations": [조문…], "problems": [알릴 문제…]}
    """
    client = law_client or LawClient(api_key(), db)
    try:
        gathered, problems = gather(client, question, cited_in_documents)
    except LawError as exc:
        gathered, problems = [], [str(exc)]
    citations = [_citation(i, article, origin) for i, (article, origin) in enumerate(gathered, start=1)]
    for citation in citations:
        db.audit("law.lookup", f"{citation['law_name']} {citation['article']}", result=citation["origin"])
    if not citations:
        db.audit("law.lookup", result="none")
        return {"text": "", "citations": [], "problems": problems}

    text = ""
    if llm is not None and getattr(llm.health(), "generation_ready", False):
        source_text = {c["index"]: c["text"][:ARTICLE_CONTEXT_CHARS] for c in citations}
        context = "\n\n".join(
            f"[{c['index']}] (「{c['law_name']}」 {c['article']}"
            + (f" {c['title']}" if c["title"] else "")
            + (f" · 시행 {c['effective_date']}" if c["effective_date"] else "") + f")\n{source_text[c['index']]}"
            for c in citations)
        prompt, _version = render("law_answer", context=context, question=question)
        data, error, _raw = llm.generate_json(prompt, ASK_SCHEMA, num_predict=ANSWER_TOKENS)
        if error:
            problems.append("조문은 조회했지만 답변 문장을 만들지 못했습니다. 아래 조문 본문을 직접 확인하세요.")
        elif isinstance(data, dict) and data.get("answered"):
            names = {c["index"]: f"{c['law_name']} {c['article']}" for c in citations}
            kept = verify_sentences(data.get("sentences") or [], source_text, names=names).kept
            # 자료 근거의 [1]과 섞이지 않게 [법1]로 적는다.
            text = " ".join(
                f"{sentence.text.rstrip()}" + "".join(f"[법{n}]" for n in sorted(set(sentence.sources)))
                for sentence in kept)
        if not text and not error:
            problems.append("조회한 조문만으로는 질문에 답하기 어렵습니다. 아래 조문 본문을 직접 확인하세요.")
    return {"text": text, "citations": citations, "problems": problems}
