"""선택한 자료 전체를 훑어 정리한다 — "요약해 줘", "쟁점을 정리해 줘".

보통의 질문은 가장 관련 있는 조각 여덟 개만 본다(search/rag.py). 그 방식으로는
자료 전체에 대한 요청에 답할 수 없다 — 여덟 조각의 요약이 될 뿐이다. 여기서는
조각을 몇 개씩 묶어 사실을 뽑고(map), 뽑은 사실로 정리를 엮는다(reduce).

근거 원칙은 같다.
  · 뽑은 사실은 문장마다 조각 번호를 달고, 규칙 검증(search/verify.py)을 지난
    것만 남긴다. 엮은 문장도 같은 검증을 한 번 더 지난다.
  · 과거 AI 답변은 재료가 아니다. 언제나 지금 선택한 원문 조각에서 시작한다.

느리다. 조각 48개면 생성 호출이 적어도 13번이다(묶음은 문서마다 따로 짠다). 그래서 자동으로 고르지 않고 사용자가
'전체 정리'를 켰을 때만 돈다.
"""

from __future__ import annotations

from ..ai.prompts_loader import render
from ..ai.schemas import MAP_SCHEMA, REDUCE_SCHEMA
from ..core import provenance
from . import intent
from .query import QueryScope
from .rag import SNIPPET_CHARS, Answer, Citation, _CITATION_MARK, _load_chunk_context
from .verify import VerifiedSentence, verify_sentences

MAX_CHUNKS = 48          # 넘으면 고르게 솎아 훑는다. 그 사실을 답에 적는다.
BATCH = 4
FACTS_PER_BATCH = 4
MAX_FACTS = 60
MAX_SENTENCES = 10
MAP_TOKENS = 600
REDUCE_TOKENS = 1000


def _chunk_ids(db, doc_ids: list[int]) -> list[int]:
    if not doc_ids:
        return []
    marks = ", ".join("?" * len(doc_ids))
    return [row[0] for row in db.con.execute(
        f"SELECT c.id FROM chunks c JOIN documents d ON d.id=c.doc_id "
        f"WHERE c.doc_id IN ({marks}) AND d.missing_since IS NULL ORDER BY c.doc_id, c.ordinal",
        doc_ids)]


def _sample(ids: list[int], limit: int) -> list[int]:
    """앞에서 자르지 않고 고르게 솎는다 — 뒤쪽 문서가 통째로 빠지면 안 된다."""
    if len(ids) <= limit:
        return ids
    step = len(ids) / limit
    return [ids[int(i * step)] for i in range(limit)]


def _batches(rows) -> list[list[int]]:
    """조각 번호를 묶음으로 나눈다. 한 묶음에는 한 문서의 조각만 넣는다.

    서로 다른 문서의 조각을 섞어 주면 경량 모델이 연도·대상을 옆 문서의 것과
    바꿔 붙인다(실측: 2023년 문서의 사실에 2022년을 붙였다).
    """
    batches: list[list[int]] = []
    for number, row in enumerate(rows, start=1):
        if batches and len(batches[-1]) < BATCH and rows[batches[-1][0] - 1]["doc_id"] == row["doc_id"]:
            batches[-1].append(number)
        else:
            batches.append([number])
    return batches


def _block(rows, numbers: list[int], source_text: dict[int, str]) -> str:
    return "\n\n".join(
        f"[{n}] ({row['filename']} · {row['locator']})\n{source_text[n]}"
        for n, row in zip(numbers, rows))


def overview(db, question: str, client, scope: QueryScope) -> Answer:
    question = question.strip()
    if not question:
        return Answer(question, "", True, error="빈 질문입니다")
    if not client.health().generation_ready:
        return Answer(question, "", True, error="전체 정리는 로컬 AI가 준비되어야 할 수 있습니다. "
                                               "설정 → 시스템에서 모델 상태를 확인하세요.")
    revision = db.get_meta("content_revision")
    every = _chunk_ids(db, scope.doc_ids or [])
    if not every:
        return Answer(question, "", True, error="정리할 자료가 아직 준비되지 않았습니다. 자료 선택과 분석 상태를 확인하세요.")
    chosen = _sample(every, MAX_CHUNKS)
    rows = _load_chunk_context(db, chosen)
    source_text: dict[int, str] = {}
    for number, row in enumerate(rows, start=1):
        # 문서는 자료이지 지시가 아니다(search/rag.py와 같은 처리).
        source_text[number] = intent.strip_instructions(" ".join(row["text"].split()))[0]
    names = {number: row["filename"] for number, row in enumerate(rows, start=1)}
    record = {"mode": "overview", "chunks": len(rows), "chunks_total": len(every), "batches": 0}

    facts: list[VerifiedSentence] = []
    for numbers in _batches(rows):
        if getattr(client, "cancelled", False):
            return Answer(question, "", True, error="질문을 취소했습니다.")
        batch = [rows[n - 1] for n in numbers]
        prompt, _version = render("map_extract", question=question, limit=str(FACTS_PER_BATCH),
                                  document=batch[0]["filename"],
                                  context=_block(batch, numbers, source_text))
        data, error, _raw = client.generate_json(prompt, MAP_SCHEMA, num_predict=MAP_TOKENS)
        record["batches"] += 1
        if error or not isinstance(data, dict):
            record["map_errors"] = record.get("map_errors", 0) + 1
            continue
        # 이 묶음에 없는 번호를 근거로 댄 사실은 버린다.
        offered = [item for item in (data.get("facts") or []) if isinstance(item, dict)
                   and all(n in numbers for n in (item.get("sources") or []) if isinstance(n, int))]
        facts.extend(verify_sentences(offered, source_text, names=names).kept)
        if len(facts) >= MAX_FACTS:
            break
    facts = facts[:MAX_FACTS]
    record["facts"] = len(facts)
    if db.get_meta("content_revision") != revision:
        return Answer(question, "", True, error="정리하는 동안 자료가 갱신되었습니다. 다시 요청해 주세요.")
    if not facts:
        return Answer(question, "확인 가능한 자료가 부족합니다.", True, model=client.gen_model,
                      generation={**record, "withheld": "no_facts"},
                      guidance="선택한 자료에서 요청과 관련된 내용을 찾지 못했습니다. "
                               "자료 선택이 맞는지 확인하거나 요청을 구체적으로 적어 보세요.")

    kept = _reduce(client, question, facts, source_text, names, record)
    if db.get_meta("content_revision") != revision:
        return Answer(question, "", True, error="정리하는 동안 자료가 갱신되었습니다. 다시 요청해 주세요.")
    text, citations = _compose(kept, rows, source_text, provenance.official_sources(
        db, [row["doc_id"] for row in rows]))
    if len(every) > len(rows):
        text += (f"\n※ 선택한 자료가 많아 전체 {len(every)}조각 가운데 {len(rows)}조각을 "
                 "고르게 골라 훑었습니다. 빠진 대목이 있을 수 있습니다.")
    return Answer(question, text, False, citations=citations, model=client.gen_model, generation=record)


def _reduce(client, question, facts, source_text, names, record) -> list[VerifiedSentence]:
    """사실들을 요청에 맞는 정리로 엮는다. 엮지 못하면 사실을 그대로 내놓는다."""
    listing = "\n".join(f"[{i}] {fact.text}" for i, fact in enumerate(facts, start=1))
    prompt, _version = render("map_reduce", question=question, facts=listing, limit=str(MAX_SENTENCES))
    data, error, _raw = client.generate_json(prompt, REDUCE_SCHEMA, num_predict=REDUCE_TOKENS)
    woven = []
    if not error and isinstance(data, dict):
        for item in data.get("sentences") or []:
            if not isinstance(item, dict):
                continue
            picked = [n for n in (item.get("sources") or []) if isinstance(n, int) and 1 <= n <= len(facts)]
            # 사실 번호를 그 사실의 원문 조각 번호로 되돌린다. 검증은 원문과 한다.
            sources = sorted({s for n in picked for s in facts[n - 1].sources})
            if sources:
                woven.append({"text": item.get("text"), "sources": sources})
    kept = verify_sentences(woven, source_text, names=names).kept[:MAX_SENTENCES]
    if kept:
        return kept
    record["reduce_fallback"] = True
    return facts[:MAX_SENTENCES]


def _compose(sentences, rows, source_text, official) -> tuple[str, list[Citation]]:
    """한 줄에 한 문장. 쓰인 근거만 등장 순서대로 번호를 다시 매긴다."""
    citations: list[Citation] = []
    renumbered: dict[int, int] = {}
    lines = []
    for sentence in sentences:
        marks = []
        for source in sentence.sources:
            if not 1 <= source <= len(rows):
                continue
            if source not in renumbered:
                row = rows[source - 1]
                renumbered[source] = len(citations) + 1
                citations.append(Citation(
                    index=renumbered[source], doc_id=row["doc_id"], filename=row["filename"],
                    locator=row["locator"], path=row["path"], snippet=source_text[source][:SNIPPET_CHARS],
                    official=provenance.describe(official.get(row["doc_id"])), chunk_id=row["chunk_id"]))
            marks.append(renumbered[source])
        body = _CITATION_MARK.sub("", sentence.text).rstrip()
        lines.append("· " + body + "".join(f"[{m}]" for m in sorted(set(marks))))
    return "\n".join(lines), citations
