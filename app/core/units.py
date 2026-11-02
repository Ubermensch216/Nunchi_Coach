"""자료 단위와 명함 — 분류의 대상을 만든다.

업무 분류는 파일 하나하나가 아니라 **의미가 같은 덩어리**를 대상으로 해야 한다. 붙임1.hwp 같은
파일은 이름에 정보가 없어서 혼자서는 분류할 수 없고, 자기 공문을 따라가야 한다. 그래서 먼저
자료를 세 가지 단위로 묶는다.

    letter  공문 한 건 — 본문 + 붙임 + 해시·문서번호·제목으로 이어지는 PC 파일
    folder  같은 폴더에 있는 PC 파일들(공문에 이어지지 않은 것)
    loose   어디에도 속하지 않는 낱장

단위마다 **명함**을 만든다. 제목, 폴더 경로, 공문 본문에서 읽은 요지·할 일·일정, 붙임 이름,
연도 같은 것이다. 분류기(규칙과 생성 모델)는 파일 전체가 아니라 이 명함을 보고 판단한다.

지키는 것
  · 확정 신호만으로 묶는다 — 해시 동일, 문서번호, 공문 키, 제목 알맹이(4자 이상, 주인이 하나일 때).
    두 공문에 같은 무게로 걸리면 붙이지 않는다.
  · 단위는 문서에서 다시 만들 수 있는 파생 자료다. 사람이 고친 값은 여기 두지 않는다.
  · 명함에는 본문에 실제로 있는 내용만 담는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import folders, gongmun, gongmun_facts, official

LETTER, FOLDER, LOOSE = "letter", "folder", "loose"
BODY, ATTACHMENT, LINKED, MEMBER = "body", "attachment", "linked", "member"
FOLDER_MIN_DOCS = 2
CARD_TITLES = 12
CARD_FACTS = 8
CARD_EXCERPTS = 3
EXCERPT_CHARS = 160
SAME_BASIS = "공문 파일과 같은 내용"
NUMBER_BASIS = "파일 이름에 공문 문서번호"
TITLE_BASIS = "파일 이름에 공문 제목"
KEY_BASIS = "같은 공문 키"


@dataclass(slots=True)
class Unit:
    kind: str
    key: str
    label: str
    docs: dict[int, tuple[str, str]] = field(default_factory=dict)   # doc_id → (역할, 근거)
    card: dict = field(default_factory=dict)

    @property
    def doc_ids(self) -> list[int]:
        return sorted(self.docs)


def _rows(db) -> list:
    return db.con.execute(
        "SELECT d.id, d.path, d.filename, d.hash, d.eff_year, s.id AS source_id, s.path AS root, s.kind AS source_kind "
        "FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE d.missing_since IS NULL ORDER BY d.id").fetchall()


def _only(owners: set[int]) -> int | None:
    return next(iter(owners)) if len(owners) == 1 else None


def _stem_key(filename: str) -> str:
    return re.sub(r"[^\w]", "", folders.compact(Path(gongmun.strip_marks(Path(filename).stem)).name))


def _letters(db, rows: list) -> tuple[list[Unit], set[int]]:
    """공문 기록마다 단위를 만들고, 확정 신호로 PC 파일을 이어 붙인다."""
    records = official.records(db)
    if not records:
        return [], set()
    by_id = {row["id"]: row for row in rows}
    units: list[Unit] = []
    for record in records:
        number = official.normalize_number(record.document_number)
        digest = next(iter(sorted(record.hashes)), "")
        key = "letter:" + (number or (digest[:16] if digest else str(record.id)))
        label = official.title_label(record.title) or record.title
        unit = Unit(LETTER, key, label)
        for doc_id in record.body_ids:
            unit.docs[doc_id] = (BODY, "")
        for doc_id in record.attachment_ids:
            unit.docs[doc_id] = (ATTACHMENT, KEY_BASIS if gongmun.key_of(
                by_id[doc_id]["filename"] if doc_id in by_id else "") else "")
        unit.card = {"number": record.document_number, "title": record.title,
                     "core": official.title_core(record.title)}
        units.append(unit)
    claimed = {doc_id for unit in units for doc_id in unit.docs}

    by_hash: dict[str, set[int]] = defaultdict(set)
    by_number: dict[str, set[int]] = defaultdict(set)
    for index, record in enumerate(records):
        for digest in record.hashes:
            by_hash[digest].add(index)
        if record.document_number:
            by_number[official.normalize_number(record.document_number)].add(index)
    cores = [unit.card["core"] for unit in units]

    def by_title(stem: str) -> int | None:
        hits = sorted(((max((len(core) for core in [cores[i]] if core and core in stem), default=0), i)
                       for i in range(len(units))), reverse=True)
        if not hits or not hits[0][0] or (len(hits) > 1 and hits[0][0] == hits[1][0]):
            return None
        return hits[0][1]

    for row in rows:
        if row["id"] in claimed or row["source_kind"] == "onnara":
            continue
        found: tuple[int, str] | None = None
        if row["hash"] and (owner := _only(by_hash.get(row["hash"], set()))) is not None:
            found = (owner, SAME_BASIS)
        elif (owner := _only({i for n in official.numbers_in(row["filename"]) for i in by_number.get(n, set())})) is not None:
            found = (owner, NUMBER_BASIS)
        elif (owner := by_title(_stem_key(row["filename"]))) is not None:
            found = (owner, TITLE_BASIS)
        if found:
            units[found[0]].docs[row["id"]] = (LINKED, found[1])
            claimed.add(row["id"])
    return units, claimed


def _folder_label(parts: list[str]) -> str:
    for name in reversed(parts):
        part = folders.parse_part(name)
        if not part.transparent:
            return part.label
    return parts[-1] if parts else ""


def _rest(rows: list, claimed: set[int]) -> list[Unit]:
    """공문에 이어지지 않은 문서를 폴더 묶음과 낱장으로 나눈다."""
    groups: dict[tuple[int, tuple[str, ...]], list] = defaultdict(list)
    loose: list = []
    for row in rows:
        if row["id"] in claimed:
            continue
        parts = tuple(folders.relative_parts(row["path"], row["root"]))
        if row["source_kind"] == "onnara" or not parts:
            loose.append((row, parts))
        else:
            groups[(row["source_id"], parts)].append(row)
    units: list[Unit] = []
    for (source_id, parts), members in groups.items():
        if len(members) < FOLDER_MIN_DOCS:
            loose.extend((row, parts) for row in members)
            continue
        label = _folder_label(list(parts))
        unit = Unit(FOLDER, f"folder:{source_id}:{'/'.join(parts).lower()}", label or "/".join(parts))
        for row in members:
            unit.docs[row["id"]] = (MEMBER, "같은 폴더")
        unit.card = {"folder": "/".join(parts)}
        units.append(unit)
    for row, parts in loose:
        title = gongmun.title_of(row["filename"]) or Path(row["filename"]).stem
        unit = Unit(LOOSE, f"loose:{row['hash'] or row['id']}", title)
        unit.docs[row["id"]] = (MEMBER, "")
        unit.card = {"folder": "/".join(parts)}
        units.append(unit)
    return units


def _titles(db, doc_ids: list[int], rows_by_id: dict) -> list[str]:
    out = []
    for doc_id in doc_ids:
        name = rows_by_id[doc_id]["filename"] if doc_id in rows_by_id else ""
        title = gongmun.title_of(name) or Path(name).stem
        if title and title not in out:
            out.append(title)
    return out[:CARD_TITLES]


def _fill_cards(db, units: list[Unit], rows_by_id: dict) -> None:
    facts_by_doc: dict[int, list[dict]] = defaultdict(list)
    for fact in gongmun_facts.facts_for_docs(db, [d for u in units if u.kind == LETTER for d in u.docs]):
        facts_by_doc[fact["doc_id"]].append(fact)
    excerpt_docs = {doc_id for unit in units if unit.kind != LETTER for doc_id in unit.doc_ids[:CARD_EXCERPTS]}
    excerpts: dict[int, str] = {}
    if excerpt_docs:
        marks = ", ".join("?" * len(excerpt_docs))
        for row in db.con.execute(
                f"SELECT c.doc_id, c.text FROM chunks c JOIN (SELECT doc_id, MIN(ordinal) AS o FROM chunks "
                f"WHERE doc_id IN ({marks}) AND trim(text) != '' GROUP BY doc_id) m "
                "ON m.doc_id = c.doc_id AND m.o = c.ordinal", sorted(excerpt_docs)):
            excerpts[row["doc_id"]] = " ".join((row["text"] or "").split())[:EXCERPT_CHARS]

    for unit in units:
        ids = unit.doc_ids
        years = sorted({rows_by_id[i]["eff_year"] for i in ids if i in rows_by_id and rows_by_id[i]["eff_year"]})
        card = dict(unit.card)
        card.update({"kind": unit.kind, "label": unit.label, "doc_count": len(ids), "years": years})
        if unit.kind == LETTER:
            bodies = [i for i, (role, _b) in unit.docs.items() if role == BODY]
            others = [i for i in ids if i not in bodies]
            card["facts"] = [{"kind": f["kind"], "text": f["text"], "due": f["due"]}
                             for i in bodies for f in facts_by_doc.get(i, [])][:CARD_FACTS]
            card["attachments"] = _titles(db, others, rows_by_id)[:8]
        else:
            card["titles"] = _titles(db, ids, rows_by_id)
            card["excerpts"] = [excerpts[i] for i in ids[:CARD_EXCERPTS] if i in excerpts]
        card.pop("core", None)
        unit.card = card


def build(db) -> list[Unit]:
    """현재 문서로 자료 단위와 명함을 만든다. DB는 고치지 않는다."""
    rows = _rows(db)
    rows_by_id = {row["id"]: row for row in rows}
    letters, claimed = _letters(db, rows)
    units = letters + _rest(rows, claimed)
    # 키가 겹치면(같은 문서번호가 두 기록에 있는 경우 등) 뒤에 번호를 붙여 구분한다.
    seen: dict[str, int] = {}
    for unit in units:
        seen[unit.key] = seen.get(unit.key, 0) + 1
        if seen[unit.key] > 1:
            unit.key = f"{unit.key}#{seen[unit.key]}"
    _fill_cards(db, units, rows_by_id)
    return units


def _fingerprint(unit: Unit, rows_by_id: dict) -> str:
    raw = unit.label + "|" + ",".join(
        f"{i}:{rows_by_id[i]['hash'] if i in rows_by_id else ''}" for i in unit.doc_ids)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def save(db, units: list[Unit] | None = None) -> dict[str, int]:
    """단위를 통째로 바꿔 저장한다. 종류별 건수를 돌려준다."""
    units = build(db) if units is None else units
    rows_by_id = {row["id"]: row for row in _rows(db)}
    with db.transaction():
        db.con.execute("DELETE FROM work_units")
        for unit in units:
            unit_id = db.con.execute(
                "INSERT INTO work_units(kind, unit_key, label, card, doc_count, fingerprint) VALUES (?,?,?,?,?,?)",
                (unit.kind, unit.key, unit.label[:120], json.dumps(unit.card, ensure_ascii=False),
                 len(unit.docs), _fingerprint(unit, rows_by_id))).lastrowid
            db.con.executemany(
                "INSERT INTO work_unit_docs(unit_id, doc_id, role, basis) VALUES (?,?,?,?)",
                [(unit_id, doc_id, role, basis) for doc_id, (role, basis) in unit.docs.items()])
    counts: dict[str, int] = {LETTER: 0, FOLDER: 0, LOOSE: 0}
    for unit in units:
        counts[unit.kind] += 1
    return counts


def load(db) -> list[dict]:
    """저장된 단위 — {id, kind, key, label, card, doc_ids}."""
    docs: dict[int, list[int]] = defaultdict(list)
    for row in db.con.execute("SELECT unit_id, doc_id FROM work_unit_docs ORDER BY doc_id"):
        docs[row["unit_id"]].append(row["doc_id"])
    return [{"id": r["id"], "kind": r["kind"], "key": r["unit_key"], "label": r["label"],
             "card": json.loads(r["card"] or "{}"), "doc_ids": docs.get(r["id"], [])}
            for r in db.con.execute("SELECT id, kind, unit_key, label, card FROM work_units ORDER BY id")]


def unit_of(db, doc_id: int) -> int | None:
    row = db.con.execute("SELECT unit_id FROM work_unit_docs WHERE doc_id = ?", (doc_id,)).fetchone()
    return row["unit_id"] if row else None
