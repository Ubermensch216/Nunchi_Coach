"""서식의 항목마다 근거 대목만으로 초안을 엮는다.

재료는 이 업무노트 대화가 인용한 원문 대목이다(대화가 없으면 선택한 자료에서
고르게 뽑은 조각). **AI가 쓴 답변 문장은 재료가 아니다** — 답변을 재료로 다시
쓰면 한 번 틀린 문장이 문서로 굳는다. 대화의 질문만 '무엇을 정리하려 했는지'의
단서로 쓴다.

항목 하나에 생성 호출 한 번이다. 받은 문장은 자료 답변과 같은 규칙 검증을
지나고, 살아남은 것이 없는 항목은 빈칸(확인 필요)으로 남긴다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..ai.prompts_loader import render
from ..ai.schemas import REDUCE_SCHEMA
from ..core import notebook
from ..search import intent
from ..search.verify import verify_sentences
from . import model
from .templates import Template

MAX_EVIDENCE = 24               # 한 문서의 재료로 쓰는 대목 수
EVIDENCE_CHARS = 700
# 한 항목의 프롬프트에 싣는 근거 전체의 글자 수. 대목 24개를 700자씩 실으면 16,800자로,
# 모델의 문맥 창(ai/client.py NUM_CTX)을 넘어 지시문이 잘려 나간다. 대목이 많으면
# 하나하나를 줄여 이 안에 담는다.
CONTEXT_CHARS = 7000
SENTENCES_PER_SECTION = 5
SECTION_TOKENS = 700


@dataclass(slots=True)
class Draft:
    title: str
    markdown: str
    citations: list[dict] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)
    empty_sections: int = 0
    note: str = ""


def evidence_from_conversation(db, notebook_id: int, snapshot: dict) -> list[dict]:
    """지금 선택한 자료로 받은 답들이 인용한 원문 대목. 같은 대목은 한 번만."""
    found: list[dict] = []
    seen: set[tuple] = set()
    for row in notebook.messages(db, notebook_id):
        if json.loads(row["source_snapshot"]) != snapshot:
            continue
        for proof in json.loads(row["citations"]):
            key = (proof.get("doc_id"), proof.get("chunk_id"), proof.get("locator"))
            if key in seen or not proof.get("exact_quote"):
                continue
            seen.add(key)
            found.append({k: proof.get(k) for k in
                          ("doc_id", "chunk_id", "filename", "locator", "path", "version_hash", "exact_quote")})
    return found


def evidence_from_sources(db, snapshot: dict, limit: int) -> list[dict]:
    """대화가 없을 때 — 선택한 자료의 조각을 고르게 뽑는다."""
    doc_ids = [int(key) for key in snapshot]
    if not doc_ids:
        return []
    marks = ", ".join("?" * len(doc_ids))
    rows = db.con.execute(
        f"SELECT c.id AS chunk_id, c.doc_id, c.locator, c.text, d.filename, d.path FROM chunks c "
        f"JOIN documents d ON d.id=c.doc_id WHERE c.doc_id IN ({marks}) ORDER BY c.doc_id, c.ordinal",
        doc_ids).fetchall()
    if len(rows) > limit:
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)]
    return [{"doc_id": r["doc_id"], "chunk_id": r["chunk_id"], "filename": r["filename"],
             "locator": r["locator"], "path": r["path"], "version_hash": snapshot[str(r["doc_id"])],
             "exact_quote": r["text"]} for r in rows]


def gather(db, notebook_id: int) -> tuple[list[dict], dict, str]:
    """(재료, 자료 선택 스냅샷, 재료의 출처에 대한 한마디)."""
    snapshot = notebook.selected_snapshot(db, notebook_id)
    if not snapshot:
        raise ValueError("왼쪽 출처에서 근거로 쓸 자료를 선택하세요.")
    evidence = evidence_from_conversation(db, notebook_id, snapshot)
    note = "이 업무노트의 대화가 인용한 원문 대목으로 엮었습니다."
    if not evidence:
        evidence = evidence_from_sources(db, snapshot, MAX_EVIDENCE)
        note = "대화 기록이 없어 선택한 자료에서 고르게 뽑은 대목으로 엮었습니다."
    if not evidence:
        raise ValueError("재료로 쓸 원문 대목이 없습니다. 자료가 준비됐는지 확인하세요.")
    if len(evidence) > MAX_EVIDENCE:
        note += f" 인용 대목 {len(evidence)}곳 가운데 최근 {MAX_EVIDENCE}곳만 썼습니다."
        evidence = evidence[-MAX_EVIDENCE:]
    return evidence, snapshot, note


def _distinct(sentences, written: set[str]) -> list:
    """같은 문장은 한 번만 쓰고 근거 번호를 모은다. 앞 항목에서 쓴 문장은 뺀다.

    경량 모델은 대목마다 같은 문장을 하나씩 내고("…제출하였다[2]", "…제출하였다[3]"),
    쓸 말이 없는 항목에도 앞 항목의 문장을 다시 내놓는다(실측).
    """
    merged: dict[str, object] = {}
    for sentence in sentences:
        key = re.sub(r"[\s.·,]", "", sentence.text)
        if key in written:
            continue
        if key in merged:
            merged[key].sources = sorted({*merged[key].sources, *sentence.sources})
        else:
            merged[key] = sentence
    written.update(merged)
    return list(merged.values())


def _where(locator: str) -> str:
    """HWPX의 위치는 꾸러미 안 파일 이름으로 적혀 있다. 문서에는 한글의 말(구역)로 옮긴다."""
    return re.sub(r"Contents/section(\d+)\.xml", lambda m: f"{int(m.group(1)) + 1}구역", locator or "")


def compose(db, notebook_id: int, template: Template, client, title: str = "", request: str = "",
            on_progress: Callable[[int, int, str], None] = lambda done, total, label: None) -> Draft:
    """초안을 엮는다. 모델이 없으면 뼈대와 근거 목록만 만든다 — 사람이 채울 수 있게."""
    evidence, snapshot, note = gather(db, notebook_id)
    book = notebook.require(db, notebook_id)
    title = title.strip() or f"{book['title']} {template.title}"
    share = min(EVIDENCE_CHARS, CONTEXT_CHARS // max(len(evidence), 1))
    source_text = {i: intent.strip_instructions(" ".join(e["exact_quote"].split())[:share])[0]
                   for i, e in enumerate(evidence, start=1)}
    names = {i: e["filename"] for i, e in enumerate(evidence, start=1)}
    context = "\n\n".join(f"[{i}] ({e['filename']} · {e['locator']})\n{source_text[i]}"
                          for i, e in enumerate(evidence, start=1))
    generating = client is not None and bool(getattr(client.health(), "generation_ready", False))
    guidance = " ".join(part for part in (template.guidance, request.strip()) if part) or "자료에 적힌 사실만 쓴다."

    body: list[tuple[str, list]] = []
    written: set[str] = set()       # 앞 항목에서 이미 쓴 문장
    for index, section in enumerate(template.sections, start=1):
        on_progress(index, len(template.sections), section.title)
        kept = []
        if generating and not getattr(client, "cancelled", False):
            prompt, _version = render(
                "studio_section", title=title, doc_type=template.doc_type, section=section.title,
                instruction=section.instruction or "이 항목에 해당하는 내용", guidance=guidance,
                limit=str(SENTENCES_PER_SECTION), context=context)
            data, error, _raw = client.generate_json(prompt, REDUCE_SCHEMA, num_predict=SECTION_TOKENS)
            if not error and isinstance(data, dict):
                kept = _distinct(verify_sentences(data.get("sentences") or [], source_text, names=names).kept,
                                 written)[:SENTENCES_PER_SECTION]
        body.append((section.title, kept))

    # 실제로 쓰인 대목만 등장 순서대로 번호를 다시 매긴다.
    renumbered: dict[int, int] = {}
    lines = [f"# {title}", ""]
    empty = 0
    for heading, sentences in body:
        lines.append(f"## {heading}")
        if not sentences:
            empty += 1
            lines.append(f"> {model.MISSING}")
        for sentence in sentences:
            marks = []
            for source in sentence.sources:
                if source in source_text:
                    marks.append(renumbered.setdefault(source, len(renumbered) + 1))
            lines.append("- " + sentence.text.rstrip() + "".join(f"[{m}]" for m in sorted(set(marks))))
        lines.append("")
    citations = [{"index": new, **evidence[old - 1]} for old, new in renumbered.items()]
    if citations:
        lines.append(f"## {model.SOURCES_HEADING}")
        lines += [f"- [{c['index']}] {c['filename']} · {_where(c['locator'])}" for c in citations]
    if not generating:
        note = "로컬 AI가 준비되지 않아 항목 뼈대만 만들었습니다. 내용을 직접 채우세요."
    return Draft(title, "\n".join(lines).strip() + "\n", citations, snapshot, empty, note)
