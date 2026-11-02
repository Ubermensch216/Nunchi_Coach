"""자료 단위를 사용자가 정한 업무 분류(사무분장 항목)에 나눈다 — 업무 파악의 첫 단계.

순서는 **확정 규칙 → 후보 좁히기 → 생성 모델 판정 → 인용으로 이어진 공문 맞추기**다.

    1. 사람이 정한 것(decided_by='user')은 그대로 둔다.
    2. 이름 규칙: 단위의 제목·폴더에 분장 항목 이름이 통째로 들어 있고 주인이 하나뿐이면 확정한다.
    3. 판정: 규칙이 못 정한 단위는 명함(제목·폴더·본문 내용)과 후보 항목을 생성 모델에 주고 하나를
       고르게 한다. 닫힌 선택지이고, 근거가 명함에 없는 답과 확신이 낮은 답은 버린다(charter_match).
       모델이 없으면 낱말 겹침이 분명한 것만 낮은 확신으로 붙이고 나머지는 '판단 불가'로 둔다.
    4. 인용 맞추기: 문서번호로 서로 인용하는 공문들은 같은 업무다. 확신 있는 짝의 항목을 이어받는다.

결과 칸은 세 가지다: 분장 항목(item) · 분장 밖(outside, 모델이 어느 항목도 아니라고 봄) ·
판단 불가(unknown). 틀린 배정보다 '분장 밖'과 '판단 불가'로 남기는 쪽이 낫다.

모델 호출은 단위마다 한 번이고 상한(MAX_CALLS)이 있다. 큰 단위부터 묻고, 같은 입력은 캐시한다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from ..core import folders, official, units
from . import charter_match

ITEM, OUTSIDE, UNKNOWN = "item", "outside", "unknown"
MAX_CALLS = 150
MAX_OPTIONS = 8
NAME_MIN = 3                 # 이름 규칙에 쓰는 항목 이름의 최소 길이(짧은 이름은 어디에나 있다)
WEAK_MIN = 2                 # 모델이 없을 때 낮은 확신으로 붙이는 최소 점수

BASIS_NAME = "이름 일치"
BASIS_AI = "AI 판단"
BASIS_WORDS = "낱말 겹침"
BASIS_CITE = "인용으로 이어진 공문"
BASIS_USER = "사용자 지정"

_WORDS = re.compile(r"[가-힣A-Za-z0-9]{2,}")


@dataclass(slots=True)
class Assignment:
    unit_key: str
    item_name: str | None
    bucket: str
    confidence: str = "low"
    basis: str = ""
    evidence: str = ""
    decided_by: str = "ai"


def score(name: str, details: list[str], text: str) -> int:
    """분장 항목이 이 글에 얼마나 나오는가. 0이면 무관."""
    flat = folders.compact(text)
    total = 0
    compact_name = folders.compact(name)
    if len(compact_name) >= 2 and compact_name in flat:
        total += 3
    else:
        tokens = _WORDS.findall(name)
        if len(tokens) >= 2 and sum(token in text for token in tokens) * 2 >= len(tokens):
            total += 2
    total += sum(1 for detail in details
                 if len(folders.compact(detail)) >= 3 and folders.compact(detail) in flat)
    return total


def _identity(card: dict, label: str) -> str:
    """단위가 무엇인지 말하는 글 — 제목·폴더. 본문 문장은 넣지 않는다."""
    return " ".join([label, card.get("title", ""), *card.get("titles", []), card.get("folder", ""),
                     *card.get("attachments", [])])


def _body_text(card: dict) -> str:
    return " ".join([*(f["text"] for f in card.get("facts", [])), *card.get("excerpts", [])])


def _by_name(options: list[dict], identity: str) -> dict | None:
    """제목·폴더에 이름이 통째로 들어 있는 항목. 가장 긴 이름이 이기고, 길이가 같은 둘이면 정하지 않는다."""
    flat = folders.compact(identity)
    hits = sorted(((len(folders.compact(o["name"])), o) for o in options
                   if len(folders.compact(o["name"])) >= NAME_MIN and folders.compact(o["name"]) in flat),
                  key=lambda pair: -pair[0])
    if not hits or (len(hits) > 1 and hits[0][0] == hits[1][0]):
        return None
    return hits[0][1]


def _candidates(options: list[dict], text: str) -> list[dict]:
    if len(options) <= MAX_OPTIONS:
        return options
    ranked = sorted(options, key=lambda o: -score(o["name"], o["details"], text))
    return ranked[:MAX_OPTIONS]


def _material(unit: dict) -> str:
    card = unit["card"]
    facts = [f"{f['text']}" for f in card.get("facts", [])]
    return charter_match.material_text(
        unit["label"], titles=card.get("titles") or ([card["title"]] if card.get("title") else []),
        folders=[card["folder"]] if card.get("folder") else [], facts=facts, excerpts=card.get("excerpts", []))


def _stored_user(db) -> dict[str, Assignment]:
    return {r["unit_key"]: Assignment(r["unit_key"], r["item_name"], r["bucket"], r["confidence"],
                                      r["basis"], r["evidence"], "user")
            for r in db.con.execute("SELECT * FROM unit_assignments WHERE decided_by = 'user'")}


def _citation_groups(db, loaded: list[dict]) -> list[list[str]]:
    """문서번호로 서로 인용하는 공문 단위들의 열쇠 묶음."""
    letters = [u for u in loaded if u["kind"] == units.LETTER]
    by_number = {official.normalize_number(u["card"].get("number", "")): u["key"]
                 for u in letters if u["card"].get("number")}
    if not by_number:
        return []
    parent = {u["key"]: u["key"] for u in letters}

    def root(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for u in letters:
        bodies = u["doc_ids"][:3]
        if not bodies:
            continue
        marks = ", ".join("?" * len(bodies))
        text = " ".join(r["text"] or "" for r in db.con.execute(
            f"SELECT text FROM chunks WHERE doc_id IN ({marks}) ORDER BY doc_id, ordinal LIMIT 40", bodies))
        for number in official.numbers_in(text):
            other = by_number.get(number)
            if other and other != u["key"]:
                parent[root(other)] = root(u["key"])
    groups: dict[str, list[str]] = defaultdict(list)
    for u in letters:
        groups[root(u["key"])].append(u["key"])
    return [keys for keys in groups.values() if len(keys) > 1]


def classify(db, client=None, max_calls: int = MAX_CALLS, on_progress=None) -> dict[str, int]:
    """저장된 자료 단위를 분류해 unit_assignments에 남긴다. 건수 요약을 돌려준다."""
    options = charter_match.charter_options(db)
    if not options:
        return {}
    expected = db.con.execute("SELECT COUNT(*) FROM documents WHERE missing_since IS NULL").fetchone()[0]
    stored_docs = db.con.execute("SELECT COUNT(DISTINCT doc_id) FROM work_unit_docs").fetchone()[0]
    if stored_docs != expected:
        units.save(db)
    loaded = units.load(db)
    names = {o["name"] for o in options}
    users = {k: a for k, a in _stored_user(db).items() if a.bucket != ITEM or a.item_name in names}

    result: dict[str, Assignment] = dict(users)
    pending: list[dict] = []
    for unit in loaded:
        if unit["key"] in result:
            continue
        card = unit["card"]
        winner = _by_name(options, _identity(card, unit["label"]))
        if winner is not None:
            result[unit["key"]] = Assignment(unit["key"], winner["name"], ITEM, "high", BASIS_NAME,
                                             f"제목·폴더에 ‘{winner['name']}’")
        else:
            pending.append(unit)

    pending.sort(key=lambda u: (u["kind"] != units.LETTER, -len(u["doc_ids"])))
    calls = skipped = 0
    for index, unit in enumerate(pending, start=1):
        card = unit["card"]
        text = _identity(card, unit["label"]) + " " + _body_text(card)
        verdict = None
        if client is not None and calls < max_calls:
            shown = _candidates(options, text)
            verdict = charter_match.judge(db, client, shown, _material(unit))
            calls += 1
        elif client is not None:
            skipped += 1
        if verdict is not None:
            if verdict.item_id is None:
                result[unit["key"]] = Assignment(unit["key"], None, OUTSIDE, verdict.confidence, BASIS_AI,
                                                 verdict.evidence)
            else:
                name = next(o["name"] for o in options if o["id"] == verdict.item_id)
                result[unit["key"]] = Assignment(unit["key"], name, ITEM, verdict.confidence, BASIS_AI,
                                                 verdict.evidence)
        else:
            ranked = sorted(((score(o["name"], o["details"], text), o) for o in options),
                            key=lambda pair: -pair[0])
            if ranked and ranked[0][0] >= WEAK_MIN and not (len(ranked) > 1 and ranked[1][0] == ranked[0][0]):
                result[unit["key"]] = Assignment(unit["key"], ranked[0][1]["name"], ITEM, "low", BASIS_WORDS,
                                                 "제목·본문에 항목 이름이나 세부업무가 나옴")
            else:
                result[unit["key"]] = Assignment(unit["key"], None, UNKNOWN)
        if on_progress:
            on_progress(index, len(pending), unit["label"])

    # 인용으로 이어진 공문은 같은 업무다. 확신 있는 짝이 가리키는 항목이 하나뿐이면 나머지가 이어받는다.
    for keys in _citation_groups(db, loaded):
        sure = {result[k].item_name for k in keys
                if k in result and result[k].bucket == ITEM and result[k].confidence in ("high", "medium")}
        if len(sure) != 1:
            continue
        name = next(iter(sure))
        for key in keys:
            current = result.get(key)
            if current is not None and current.decided_by == "ai" and not (
                    current.bucket == ITEM and current.confidence in ("high", "medium")):
                result[key] = Assignment(key, name, ITEM, "medium", BASIS_CITE, "문서번호를 서로 인용하는 공문")

    with db.transaction():
        db.con.execute("DELETE FROM unit_assignments WHERE decided_by = 'ai'")
        # 분장에서 사라진 항목을 가리키는 사람의 지정은 쓸 수 없다.
        stale = [k for k, a in _stored_user(db).items() if k not in users]
        db.con.executemany("DELETE FROM unit_assignments WHERE unit_key = ?", [(k,) for k in stale])
        for a in result.values():
            if a.decided_by == "ai":
                db.con.execute(
                    "INSERT INTO unit_assignments(unit_key, item_name, bucket, confidence, basis, evidence, decided_by) "
                    "VALUES (?,?,?,?,?,?, 'ai') ON CONFLICT(unit_key) DO NOTHING",
                    (a.unit_key, a.item_name, a.bucket, a.confidence, a.basis, a.evidence))
    counts = {ITEM: 0, OUTSIDE: 0, UNKNOWN: 0}
    for a in result.values():
        counts[a.bucket] += 1
    counts["model_calls"] = calls
    counts["not_judged"] = skipped
    return counts


def set_user_assignment(db, unit_key: str, item_name: str | None, bucket: str | None = None) -> None:
    """사람이 단위의 분류를 정한다. item_name이 None이면 분장 밖으로 둔다. 재분류가 덮어쓰지 않는다."""
    bucket = bucket or (ITEM if item_name else OUTSIDE)
    with db.transaction():
        db.con.execute(
            "INSERT INTO unit_assignments(unit_key, item_name, bucket, confidence, basis, decided_by) "
            "VALUES (?,?,?, 'high', ?, 'user') ON CONFLICT(unit_key) DO UPDATE SET item_name=excluded.item_name, "
            "bucket=excluded.bucket, confidence='high', basis=excluded.basis, decided_by='user', updated_at=datetime('now')",
            (unit_key, item_name if bucket == ITEM else None, bucket, BASIS_USER))


def assignments(db) -> dict[str, Assignment]:
    return {r["unit_key"]: Assignment(r["unit_key"], r["item_name"], r["bucket"], r["confidence"], r["basis"],
                                      r["evidence"], r["decided_by"])
            for r in db.con.execute("SELECT * FROM unit_assignments")}


def by_item(db) -> dict[str, list[dict]]:
    """분장 항목 이름 → 그 항목으로 분류된 단위들(저장된 단위 정보 포함)."""
    mine = assignments(db)
    out: dict[str, list[dict]] = defaultdict(list)
    for unit in units.load(db):
        a = mine.get(unit["key"])
        if a is not None and a.bucket == ITEM and a.item_name:
            out[a.item_name].append({**unit, "assignment": a})
    return out
