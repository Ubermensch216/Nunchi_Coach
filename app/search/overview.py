"""선택한 자료 전체를 훑어 정리한다 — "요약해 줘", "쟁점을 정리해 줘".

보통의 질문은 가장 관련 있는 조각 여덟 개만 본다(search/rag.py). 그 방식으로는
자료 전체에 대한 요청에 답할 수 없다 — 여덟 조각의 요약이 될 뿐이다. 여기서는
조각을 몇 개씩 묶어 사실을 뽑고(map), 뽑은 사실로 정리를 엮는다(reduce).

근거 원칙은 같다.
  · 뽑은 사실은 문장마다 조각 번호를 달고, 규칙 검증(search/verify.py)을 지난
    것만 남긴다. 엮은 문장도 같은 검증을 한 번 더 지난다.
  · 과거 AI 답변은 재료가 아니다. 언제나 지금 선택한 원문 조각에서 시작한다.

느리다. 조각 48개면 생성 호출이 적어도 13번이다(묶음은 문서마다 따로 짠다). 그래서 보통
질문은 이쪽으로 오지 않는다. 주제 없이 "요약해 줘"라고만 한 요청은 자료 크기와 상관없이
이쪽으로 온다(automatic) — 그런 요청을 조각 몇 개만 보는 보통 질문으로 받으면 모델이
"답할 수 없다"고 하고, 화면에는 '자료 부족'으로 보인다(실측: 24행짜리 키워드 목록 시트에
"요약해줘"). 걸리는 시간은 MAX_CHUNKS가 묶고, 화면은 묶음 진행을 보여 준다. 주제가 있는
요청은 사용자가 답 아래의 '자료 전체를 훑어 다시 정리'를 눌렀을 때 온다.
"""

from __future__ import annotations

import re

from ..ai.prompts_loader import render
from ..ai.schemas import MAP_SCHEMA, REDUCE_SCHEMA
from ..core import provenance
from . import intent
from .query import QueryScope
from .rag import (SNIPPET_CHARS, Answer, Citation, _CITATION_MARK, _complete_sentences,
                  _load_chunk_context, context_head)
from .verify import VerifiedSentence, verify_sentences

MAX_CHUNKS = 48          # 넘으면 고르게 솎아 훑는다. 그 사실을 답에 적는다.
BATCH = 4
# 한 묶음의 글자 수 상한. 조각 넷이 모두 길면(1,200자씩) 숫자 많은 표는 4,000토큰에 닿아
# 모델의 기본 문맥 창(4,096)을 넘고, 그러면 프롬프트 앞쪽의 지시문이 오류 없이 잘린다.
BATCH_CHARS = 3000
FACTS_PER_BATCH = 4
MAX_FACTS = 60
MAX_SENTENCES = 10
MAP_TOKENS = 600
REDUCE_TOKENS = 1000
# 개수 상한은 프롬프트로만 말하면 지켜지지 않는다(실측: 4개라고 했는데 행마다 하나씩
# 열몇 개를 쓰다 생성 한도에 끊겼다). 문법으로 막는다.
_MAP_SCHEMA = {**MAP_SCHEMA, "properties": {
    "facts": {**MAP_SCHEMA["properties"]["facts"], "maxItems": FACTS_PER_BATCH}}}
_REDUCE_SCHEMA = {**REDUCE_SCHEMA, "properties": {
    "sentences": {**REDUCE_SCHEMA["properties"]["sentences"], "maxItems": MAX_SENTENCES}}}

_SUMMARY = re.compile(r"요약|정리|개요|간추|줄거리|(?:무슨|어떤|무엇에|뭐에)\s*(?:관한\s*|대한\s*)?(?:내용|자료|문서|파일)")
# 요청에서 주제가 될 수 없는 말(앞머리로 견준다). 이것들을 빼고 남는 낱말이 없으면
# 특정 주제가 아니라 자료 전체를 묻는 것이다.
_NOT_A_TOPIC = (
    "요약", "정리", "개요", "간추", "줄거리", "설명", "문서", "자료", "파일", "보고서", "엑셀", "시트",
    "내용", "전체", "전부", "모두", "모든", "선택", "첨부", "올린", "업로드", "간단", "간략", "짧", "쉽",
    "알기", "한눈", "핵심", "주요", "중요", "위주", "해줘", "해주", "해봐", "줘", "주세요", "주십",
    "부탁", "알려", "말해", "보여", "뭐", "무슨", "무엇", "어떤", "관한", "대한", "대해", "좀", "한번",
    "다시", "것", "건지", "인지", "있", "줄", "문장", "한국어", "우리말", "이거", "이것", "그거", "그것")
_POINTERS = {"이", "그", "저", "위", "이번", "해당", "한", "두", "세", "몇", "해", "해서"}


def asks_whole(question: str) -> bool:
    """특정 주제 없이 자료 전체를 요약·정리해 달라는 요청인가(규칙으로만 판정한다).

    "요약해줘", "이 파일 무슨 내용이야?"는 참이고, "예산 집행 내용을 정리해 줘"는
    주제('예산', '집행')가 있어 거짓이다 — 그런 요청은 관련 대목을 찾는 보통 질문이 낫다.
    """
    if not _SUMMARY.search(question):
        return False
    for word in re.findall(r"[가-힣A-Za-z]+", question):
        if word in _POINTERS or any(word.startswith(filler) for filler in _NOT_A_TOPIC):
            continue
        return False
    return True


def automatic(db, question: str, doc_ids: list[int]) -> bool:
    """사용자가 고르지 않았어도 전체 정리로 받을 요청인가."""
    return asks_whole(question) and bool(_chunk_ids(db, doc_ids))


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
    size = 0
    for number, row in enumerate(rows, start=1):
        if (batches and len(batches[-1]) < BATCH and size + len(row["text"]) <= BATCH_CHARS
                and rows[batches[-1][0] - 1]["doc_id"] == row["doc_id"]):
            batches[-1].append(number)
            size += len(row["text"])
        else:
            batches.append([number])
            size = len(row["text"])
    return batches


def _readable(text: str) -> str:
    """모델에 보여 줄 조각 글. 시트 조각은 행과 칸의 경계를 남긴다 — 한 줄로 펴면
    어디까지가 한 행인지 알 수 없어 모델이 행을 통째로 베껴 적는다."""
    if "\t" not in text:
        return " ".join(text.split())
    lines = (" | ".join(" ".join(cell.split()) for cell in line.split("\t")) for line in text.splitlines())
    return "\n".join(line for line in lines if line.strip(" |"))


OUTLINE_NAMES = 12       # 표의 개요에 적을 항목 이름 수
OUTLINE_NAME_CHARS = 30  # 이보다 긴 칸은 이름이 아니라 내용이다


def _sheet_outlines(rows, complete: bool) -> list[VerifiedSentence]:
    """시트마다 한 문장 — 열 이름, 행 수, 항목 이름들. 모델에 묻지 않고 조각에서 읽는다.

    경량 모델은 표를 요약하라고 해도 행을 하나씩 베껴 적는다(실측: 20행짜리 목록의
    '요약'이 앞 여덟 행의 사본이었다). 표가 무엇을 담았는지는 머리 행과 첫 글자 열이
    이미 말해 준다. 머리 행을 알 수 없는 시트는 건드리지 않는다.
    complete가 거짓이면(조각을 솎아 훑었다) 행 수는 적지 않는다.
    """
    sheets: dict[tuple[int, str], list[tuple[int, list[list[str]]]]] = {}
    for number, row in enumerate(rows, start=1):
        lines = row["text"].splitlines()
        if "\t" not in row["text"] or not lines or not re.fullmatch(r"\[.+\]", lines[0]):
            continue
        table = [[cell.strip() for cell in line.split("\t")] for line in lines[1:] if "\t" in line]
        sheets.setdefault((row["doc_id"], lines[0][1:-1]), []).append((number, table))

    outlines = []
    for (_doc, title), chunks in sheets.items():
        header = next((line for line in chunks[0][1] if sum(map(bool, line)) >= 2), None)
        if header is None or any(re.fullmatch(r"[\d.,%\-]+", cell) for cell in header if cell):
            continue
        # 이어지는 조각마다 되풀이해 붙인 머리 행은 자료가 아니다(ingest/parsers/xlsx.py).
        body = [(number, line) for number, table in chunks for line in table if line != header]
        if len(body) < 2:
            continue
        named = next((i for i, cell in enumerate(header) if cell and _is_name_column(
            [line[i] for _n, line in body if i < len(line)])), None)
        columns = "·".join(cell for cell in header if cell)
        text = f"'{title}' 시트는 {columns} 열로 된 표"
        text += f"이며 {len(body)}개 행이 있다." if complete else "이다."
        sources = [chunks[0][0]]
        if named is not None:
            listed = [(number, line[named]) for number, line in body if named < len(line) and line[named]]
            shown = listed[:OUTLINE_NAMES]
            text += f" {header[named]}: " + ", ".join(name for _n, name in shown)
            text += " 등." if len(listed) > len(shown) else "."
            sources += [number for number, _name in shown]
        outlines.append(VerifiedSentence(text=text, sources=sorted(set(sources))))
    return outlines


def _is_name_column(values: list[str]) -> bool:
    """항목 이름이 적힌 열인가 — 대부분 짧은 글자이고 숫자가 아니다."""
    filled = [value for value in values if value]
    names = [value for value in filled
             if len(value) <= OUTLINE_NAME_CHARS and not re.fullmatch(r"[\d.,%\-\s]+", value)]
    return bool(filled) and len(names) >= len(filled) * 0.7


def _block(rows, numbers: list[int], source_text: dict[int, str]) -> str:
    return "\n\n".join(
        f"[{n}] ({context_head(row)})\n{source_text[n]}"
        for n, row in zip(numbers, rows))


def overview(db, question: str, client, scope: QueryScope, progress=None) -> Answer:
    """progress(끝난 묶음 수, 전체 묶음 수)는 묶음 하나를 시작하기 전마다 불린다."""
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
        source_text[number] = intent.strip_instructions(_readable(row["text"]))[0]
    names = {number: row["filename"] for number, row in enumerate(rows, start=1)}
    record = {"mode": "overview", "chunks": len(rows), "chunks_total": len(every), "batches": 0}

    facts: list[VerifiedSentence] = []
    batches = _batches(rows)
    for numbers in batches:
        if getattr(client, "cancelled", False):
            return Answer(question, "", True, error="질문을 취소했습니다.")
        if progress:
            progress(record["batches"], len(batches))
        batch = [rows[n - 1] for n in numbers]
        prompt, _version = render("map_extract", question=question, limit=str(FACTS_PER_BATCH),
                                  document=batch[0]["filename"],
                                  context=_block(batch, numbers, source_text))
        data, error, raw = client.generate_json(prompt, _MAP_SCHEMA, num_predict=MAP_TOKENS)
        record["batches"] += 1
        if data is None and raw:
            # 생성 한도에 걸려 끊겼어도 끝까지 적힌 사실은 쓴다(search/rag.py와 같은 처리).
            # 통째로 버리면 그 묶음의 조각들이 정리에서 빠진다.
            complete = _complete_sentences(raw)
            if complete:
                data, error = {"facts": complete}, None
                record["map_recovered"] = record.get("map_recovered", 0) + 1
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
    # 자료 전체를 묻는 요청이면 표가 어떻게 생겼는지부터 말한다. 규칙으로 읽은 것이다.
    outlines = _sheet_outlines(rows, complete=len(every) == len(rows)) if asks_whole(question) else []
    if outlines:
        record["sheet_outlines"] = len(outlines)
    if not facts and not outlines:
        guidance = ("선택한 자료에서 요청과 관련된 내용을 찾지 못했습니다. "
                    "자료 선택이 맞는지 확인하거나 요청을 구체적으로 적어 보세요.")
        return Answer(question, "확인 가능한 자료가 부족합니다.", True, model=client.gen_model,
                      generation={**record, "withheld": "no_facts", "guidance": guidance},
                      guidance=guidance)

    kept = outlines + (_reduce(client, question, facts, source_text, names, record) if facts else [])
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
    data, error, _raw = client.generate_json(prompt, _REDUCE_SCHEMA, num_predict=REDUCE_TOKENS)
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
