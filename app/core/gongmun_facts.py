"""공문 본문에서 요지·해야 할 일·일정·제출처를 읽는다.

공문 본문에는 주요 내용과 해야 할 일, 일정이 들어 있다. 파일 이름이나 날짜 분포만으로는 이것을
알 수 없어서, 본문 문장을 읽어 근거와 함께 남긴다.

지키는 것
  · **본문에 있는 문장만 쓴다.** 모든 항목은 근거 문장(evidence)을 갖고, 그 문장은 본문에 실제로 있다.
    모델이 낸 항목도 근거가 본문에 없으면 버린다.
  · 규칙이 먼저고 모델은 보완이다. 모델이 없거나 실패해도 규칙으로 읽은 결과는 남는다.
  · 날짜는 본문에 적힌 것만 읽는다. 해가 없으면 문서의 시점 연도를 쓰고, 그것도 없으면 날짜를 비운다.
  · 사람이 고친 항목(decided_by='user')은 다시 읽어도 지우지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from . import gongmun

SUMMARY, TODO, SCHEDULE, SUBMIT = "summary", "todo", "schedule", "submit"
KIND_LABELS = {SUMMARY: "요지", TODO: "해야 할 일", SCHEDULE: "일정", SUBMIT: "제출처·방법"}
MAX_FACTS = 12
TEXT_LIMIT = 8000
MODEL_MARK = "|model"


@dataclass(slots=True)
class Fact:
    kind: str
    text: str
    evidence: str
    due: str | None = None


_FULL_DATE = re.compile(r"((?:19|20)\d{2})\s*[.년]\s*(\d{1,2})\s*[.월]\s*(\d{1,2})\s*일?")
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_DOT_DAY = re.compile(r"(?<![\d.])(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*\(")
_HEADER = re.compile(r"^\s*(수신|경유|참조|발신|기안자?|시행|접수|결재|협조자|우\s*\d{5})\b")
_TITLE = re.compile(r"^\s*제\s*목\s*[:：]?\s*(.+)$")
_ITEM = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*(.+)$")
_SENTENCE_END = re.compile(r"(?<=[다요음함됨])\.\s+")

_ASK = re.compile(r"바랍니다|바랍니다|주시기\s*바|요청드립니다|하여\s*주십시오|해\s*주십시오|하시기\s*바|협조|부탁")
_VERBS = re.compile(r"제출|회신|통보|송부|참석|참여|작성|신청|보고|확인|검토|통지|등록|입력|준비|게시|이행|시행|실시|제공|취합")
_DEADLINE = re.compile(r"일시|기한|까지|기간|개최|실시|마감|이내|예정|제출일|접수")
_PLACE = re.compile(
    r"제출처|제출\s*방법|(?:[가-힣A-Za-z0-9]+(?:로|으로|에|에게))\s*(?:제출|회신|송부|발송)|"
    r"이메일|e-?mail|[\w.]+@[\w.]+|업무관리시스템|온나라|공문으로", re.IGNORECASE)


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _dates(sentence: str, year: int | None) -> list[str]:
    found: list[str] = []

    def add(y: int | None, m: int, d: int) -> None:
        if y is None:
            return
        try:
            found.append(date(y, m, d).isoformat())
        except ValueError:
            pass

    consumed = []
    for m in _FULL_DATE.finditer(sentence):
        add(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        consumed.append(m.span())
    for pattern in (_MONTH_DAY, _DOT_DAY):
        for m in pattern.finditer(sentence):
            if any(a <= m.start() < b for a, b in consumed):
                continue
            add(year, int(m.group(1)), int(m.group(2)))
    return found


def has_date(sentence: str) -> bool:
    return bool(_FULL_DATE.search(sentence) or _MONTH_DAY.search(sentence) or _DOT_DAY.search(sentence))


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if not line or _HEADER.match(line):
            continue
        out.extend(part.strip() for part in _SENTENCE_END.split(line + " ") if part.strip())
    return out


def extract_rules(text: str, year: int | None = None) -> list[Fact]:
    """본문 문장에서 규칙으로 읽는다. 모델이 필요 없다."""
    text = (text or "")[:TEXT_LIMIT]
    sentences = _sentences(text)
    facts: list[Fact] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, sentence: str, due: str | None = None) -> None:
        key = (kind, _compact(sentence))
        if key in seen or len(sentence) < 6:
            return
        seen.add(key)
        facts.append(Fact(kind, sentence[:200], sentence, due))

    summary = ""
    for sentence in sentences:
        item = _ITEM.match(sentence)
        body = item.group(2).strip() if item else ""
        if body and not body.startswith(("관련", "붙임")) and len(body) >= 12:
            summary = sentence
            break
    if not summary:
        for line in text.splitlines():
            title = _TITLE.match(line)
            if title and len(title.group(1).strip()) >= 6:
                summary = " ".join(line.split())
                break
    if summary:
        add(SUMMARY, summary)

    for sentence in sentences:
        if sentence == summary and not (_ASK.search(sentence) or has_date(sentence)):
            continue
        dates = _dates(sentence, year)
        if has_date(sentence) and _DEADLINE.search(sentence):
            add(SCHEDULE, sentence, dates[-1] if dates else None)
        if _PLACE.search(sentence) and _VERBS.search(sentence):
            add(SUBMIT, sentence)
        elif _ASK.search(sentence) and _VERBS.search(sentence):
            add(TODO, sentence, dates[-1] if dates else None)
    return facts[:MAX_FACTS]


def extract_with_model(text: str, client, year: int | None = None) -> list[Fact]:
    """생성 모델로 보완한다. 근거 문장이 본문에 없는 항목은 버린다. 실패하면 빈 목록이다."""
    from ..ai.prompts_loader import render
    from ..ai.schemas import GONGMUN_FACTS_SCHEMA

    body = (text or "")[:TEXT_LIMIT]
    prompt, _version = render("gongmun_facts", text=body)
    data, error, _raw = client.generate_json(prompt, GONGMUN_FACTS_SCHEMA, num_predict=900)
    if error or not isinstance(data, dict):
        return []
    flat = _compact(body)
    out: list[Fact] = []
    for raw in data.get("facts") or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        evidence = " ".join(str(raw.get("evidence") or "").split())
        said = " ".join(str(raw.get("text") or "").split())
        if kind not in KIND_LABELS or len(evidence) < 6 or _compact(evidence) not in flat or not said:
            continue
        dates = _dates(evidence, year)
        out.append(Fact(kind, said[:200], evidence[:300], dates[-1] if dates else None))
    return out


def _merge(first: list[Fact], second: list[Fact]) -> list[Fact]:
    """규칙 결과를 앞에 두고, 같은 근거 문장은 한 번만 남긴다."""
    seen = {(f.kind, _compact(f.evidence)) for f in first}
    merged = list(first)
    for fact in second:
        if (fact.kind, _compact(fact.evidence)) not in seen:
            merged.append(fact)
            seen.add((fact.kind, _compact(fact.evidence)))
    return merged[:MAX_FACTS]


def document_text(db, doc_id: int) -> str:
    text = "\n".join(row["text"] for row in db.con.execute(
        "SELECT text FROM document_sections WHERE doc_id = ? ORDER BY ordinal", (doc_id,)) if row["text"])
    if text.strip():
        return text
    return "\n".join(row["text"] for row in db.con.execute(
        "SELECT text FROM chunks WHERE doc_id = ? ORDER BY ordinal", (doc_id,)) if row["text"])


def body_doc_ids(db) -> list[int]:
    """공문 본문 문서 — 꾸러미로 받은 본문과 폴더의 '[본문]' 표시 파일."""
    ids = {row["doc_id"] for row in db.con.execute(
        "SELECT a.doc_id FROM source_assets a JOIN documents d ON d.id = a.doc_id "
        "WHERE a.role = 'body' AND a.doc_id IS NOT NULL AND d.missing_since IS NULL")}
    for row in db.con.execute(
            "SELECT id, filename FROM documents WHERE missing_since IS NULL AND filename LIKE '%본문%'"):
        if gongmun.is_body(row["filename"]):
            ids.add(row["id"])
    return sorted(ids)


def analyze_document(db, doc_id: int, client=None) -> int:
    """한 본문 문서를 읽어 저장한다. 저장한 항목 수를 돌려준다."""
    row = db.con.execute("SELECT eff_year FROM documents WHERE id = ?", (doc_id,)).fetchone()
    year = row["eff_year"] if row else None
    text = document_text(db, doc_id)
    facts = extract_rules(text, year)
    if client is not None and text.strip():
        facts = _merge(facts, extract_with_model(text, client, year))
    with db.transaction():
        db.con.execute("DELETE FROM official_facts WHERE doc_id = ? AND decided_by = 'ai'", (doc_id,))
        for fact in facts:
            db.con.execute(
                "INSERT INTO official_facts(doc_id, kind, text, due, evidence) VALUES (?,?,?,?,?)",
                (doc_id, fact.kind, fact.text, fact.due, fact.evidence))
    return len(facts)


def analyze_bodies(db, client=None, on_progress=None, is_stopped=None) -> int:
    """아직 읽지 않았거나 내용이 바뀐 본문을 모두 읽는다. 읽은 문서 수를 돌려준다."""
    ids = body_doc_ids(db)
    done = 0
    for index, doc_id in enumerate(ids, start=1):
        if is_stopped is not None and is_stopped():
            break
        row = db.con.execute("SELECT hash, filename FROM documents WHERE id = ?", (doc_id,)).fetchone()
        stamp = f"{row['hash'] or ''}{MODEL_MARK if client is not None else ''}"
        key = f"gongmun_facts:{doc_id}"
        stored = db.get_meta(key) or ""
        if stored == stamp or (stored and stored.startswith(row["hash"] or "?") and client is None):
            if on_progress:
                on_progress(index, len(ids), row["filename"])
            continue
        analyze_document(db, doc_id, client)
        db.set_meta(key, stamp)
        done += 1
        if on_progress:
            on_progress(index, len(ids), row["filename"])
    return done


def facts_for_docs(db, doc_ids: list[int], kinds: tuple[str, ...] | None = None) -> list[dict]:
    """문서들의 본문 내용 — {doc_id, kind, text, due, evidence, filename}."""
    if not doc_ids:
        return []
    marks = ", ".join("?" * len(doc_ids))
    sql = ("SELECT f.doc_id, f.kind, f.text, f.due, f.evidence, d.filename FROM official_facts f "
           f"JOIN documents d ON d.id = f.doc_id WHERE f.doc_id IN ({marks})")
    params: list = list(doc_ids)
    if kinds:
        sql += f" AND f.kind IN ({', '.join('?' * len(kinds))})"
        params += list(kinds)
    return [dict(r) for r in db.con.execute(sql + " ORDER BY f.doc_id, f.id", params)]


def facts_for_task(db, task_id: int, kinds: tuple[str, ...] | None = None) -> list[dict]:
    ids = [r["doc_id"] for r in db.con.execute("SELECT doc_id FROM task_docs WHERE task_id = ?", (task_id,))]
    return facts_for_docs(db, ids, kinds)
