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
OUTSIDE_NAME, UNKNOWN_NAME = "분장 밖", "판단 불가"
MAX_CALLS = 150
MAX_PART_CALLS = 100        # 분장 항목 안의 세부업무를 모델에 묻는 횟수의 상한
# 모델에게 보일 분장 항목 수. 팀 전체 분장표는 40개 안팎이다 — 몇 개만 잘라 보이면 맞는 항목이 목록에서
# 빠진다(2026-11-02 실자료: 김명진 행의 'IT 지원'이 40개 가운데 39번째라 8개 후보에 들지 못했다).
MAX_OPTIONS = 40
NAME_MIN = 3                 # 이름 규칙에 쓰는 항목 이름의 최소 길이(짧은 이름은 어디에나 있다)
WEAK_MIN = 3                 # 모델이 없을 때 붙이는 최소 점수 — 항목 이름이나 세부업무가 통째로 나와야 한다

BASIS_NAME = "이름 일치"
BASIS_AI = "AI 판단"
BASIS_AI_REJECTED = "AI 답 보류(근거 불일치·확신 낮음)"
BASIS_WORDS = "낱말 겹침"
BASIS_CITE = "인용으로 이어진 공문"
BASIS_USER = "사용자 지정"
BASIS_TAUGHT = "사용자가 고친 폴더·제목"

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
    part: str = ""


def score(name: str, details: list[str], text: str) -> int:
    """분장 항목이 이 글에 얼마나 나오는가. 0이면 무관."""
    flat = folders.compact(text)
    total = 0
    compact_name = folders.compact(name)
    if len(compact_name) >= 2 and compact_name in flat:
        total += 3
    else:
        # 부분 일치는 3자 이상 낱말로만 본다. '감사'·'업무'·'지원' 같은 두 글자는 공문 어디에나 있어서
        # '사회복지분야 감사'가 IT 공문에 붙었다(2026-11-02 실자료).
        tokens = [token for token in _WORDS.findall(name) if len(token) >= 3]
        hits = sum(token in text for token in tokens)
        if hits and hits * 2 >= len(tokens):
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


def _by_name(options: list[dict], identity: str, min_len: int = NAME_MIN) -> dict | None:
    """제목·폴더에 이름이 통째로 들어 있는 항목. 가장 긴 이름이 이기고, 길이가 같은 둘이면 정하지 않는다."""
    flat = folders.compact(identity)
    hits = sorted(((len(folders.compact(o["name"])), o) for o in options
                   if len(folders.compact(o["name"])) >= min_len and folders.compact(o["name"]) in flat),
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
                                      r["basis"], r["evidence"], "user", r["part"])
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


def _taught(loaded: list[dict], users: dict[str, Assignment]) -> tuple[dict, dict]:
    """사람이 고친 단위에서 규칙을 배운다 — 같은 폴더(와 그 아래)·같은 공문 제목은 같은 분류.

    같은 폴더나 제목을 서로 다른 분류로 고쳤으면 그 규칙은 쓰지 않는다(애매하다).
    값은 분장 항목 이름이고, None은 '분장 밖'이다.
    """
    folder_rules: dict[str, set] = defaultdict(set)
    label_rules: dict[str, set] = defaultdict(set)
    by_key = {u["key"]: u for u in loaded}
    for key, assignment in users.items():
        unit = by_key.get(key)
        if unit is None or assignment.bucket == UNKNOWN:
            continue
        target = assignment.item_name if assignment.bucket == ITEM else None
        folder = (unit["card"].get("folder") or "").lower()
        if folder:
            folder_rules[folder].add(target)
        if unit["kind"] != units.FOLDER:
            label = folders.compact(unit["label"])
            if len(label) >= 4:
                label_rules[label].add(target)
    return ({k: next(iter(v)) for k, v in folder_rules.items() if len(v) == 1},
            {k: next(iter(v)) for k, v in label_rules.items() if len(v) == 1})


def _apply_taught(unit: dict, folder_rules: dict, label_rules: dict) -> tuple[str | None, str] | None:
    folder = (unit["card"].get("folder") or "").lower()
    if folder:
        best = max((f for f in folder_rules if folder == f or folder.startswith(f + "/")), key=len, default=None)
        if best is not None:
            return folder_rules[best], f"사용자가 폴더 ‘{best}’를 고침"
    if unit["kind"] != units.FOLDER:
        label = folders.compact(unit["label"])
        if label in label_rules:
            return label_rules[label], f"사용자가 같은 제목 ‘{unit['label']}’을 고침"
    return None


def classify(db, client=None, max_calls: int = MAX_CALLS, on_progress=None,
             max_part_calls: int = MAX_PART_CALLS) -> dict[str, int]:
    """저장된 자료 단위를 분류해 unit_assignments에 남긴다. 건수 요약을 돌려준다."""
    options = charter_match.charter_options(db)
    if not options:
        return {}
    expected = db.con.execute("SELECT COUNT(*) FROM documents d JOIN sources s ON s.id = d.source_id WHERE d.missing_since IS NULL AND s.kind != 'upload'").fetchone()[0]
    stored_docs = db.con.execute("SELECT COUNT(DISTINCT doc_id) FROM work_unit_docs").fetchone()[0]
    if stored_docs != expected:
        units.save(db)
    loaded = units.load(db)
    model_client = client
    names = {o["name"] for o in options}
    users = {k: a for k, a in _stored_user(db).items() if a.bucket != ITEM or a.item_name in names}

    result: dict[str, Assignment] = dict(users)
    folder_rules, label_rules = _taught(loaded, users)
    pending: list[dict] = []
    for unit in loaded:
        if unit["key"] in result:
            continue
        card = unit["card"]
        taught = _apply_taught(unit, folder_rules, label_rules)
        if taught is not None:
            name, where = taught
            result[unit["key"]] = (Assignment(unit["key"], name, ITEM, "high", BASIS_TAUGHT, where) if name else
                                   Assignment(unit["key"], None, OUTSIDE, "high", BASIS_TAUGHT, where))
            continue
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
        if verdict is not None and verdict.rejected:
            result[unit["key"]] = Assignment(unit["key"], None, UNKNOWN, "low", BASIS_AI_REJECTED,
                                             verdict.evidence)
        elif verdict is not None:
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

    parts = _assign_parts(db, model_client, options, loaded, result, max_part_calls)
    with db.transaction():
        db.con.execute("DELETE FROM unit_assignments WHERE decided_by = 'ai'")
        # 분장에서 사라진 항목을 가리키는 사람의 지정은 쓸 수 없다.
        stale = [k for k, a in _stored_user(db).items() if k not in users]
        db.con.executemany("DELETE FROM unit_assignments WHERE unit_key = ?", [(k,) for k in stale])
        for a in result.values():
            if a.decided_by == "ai":
                db.con.execute(
                    "INSERT INTO unit_assignments(unit_key, item_name, bucket, confidence, basis, evidence, decided_by, part) "
                    "VALUES (?,?,?,?,?,?, 'ai', ?) ON CONFLICT(unit_key) DO NOTHING",
                    (a.unit_key, a.item_name, a.bucket, a.confidence, a.basis, a.evidence, parts.get(a.unit_key, "")))
        for key, a in result.items():
            if a.decided_by == "user" and a.bucket == ITEM:
                db.con.execute("UPDATE unit_assignments SET part = ? WHERE unit_key = ?", (parts.get(key, ""), key))
    counts = {ITEM: 0, OUTSIDE: 0, UNKNOWN: 0}
    for a in result.values():
        counts[a.bucket] += 1
    counts["model_calls"] = calls
    counts["not_judged"] = skipped
    return counts


def _assign_parts(db, client, options: list[dict], loaded: list[dict], result: dict[str, Assignment],
                  max_calls: int = MAX_PART_CALLS) -> dict[str, str]:
    """분장 항목에 세부업무가 적혀 있으면, 그 항목으로 나뉜 단위가 어느 세부업무인지 정한다.

    항목 이름과 같은 방식이다 — 제목·폴더에 세부업무 이름이 통째로 있으면 규칙으로, 아니면 모델이
    세부업무 가운데 하나(또는 해당 없음)를 고른다. 해당 없음은 항목 전체에 속한 자료로 둔다.
    """
    details = {o["name"]: [{"id": n, "name": d, "details": []} for n, d in enumerate(o["details"], start=1)]
               for o in options if len(o["details"]) >= 2}
    out: dict[str, str] = {}
    calls = 0
    for unit in sorted(loaded, key=lambda u: -len(u["doc_ids"])):
        a = result.get(unit["key"])
        if a is None or a.bucket != ITEM or a.item_name not in details:
            continue
        choices = details[a.item_name]
        card = unit["card"]
        winner = _by_name(choices, _identity(card, unit["label"]), min_len=2)
        if winner is None and client is not None and calls < max_calls:
            text = _identity(card, unit["label"]) + " " + _body_text(card)
            verdict = charter_match.judge(db, client, _candidates(choices, text), _material(unit))
            calls += 1
            if verdict is not None and verdict.item_id is not None:
                winner = next(c for c in choices if c["id"] == verdict.item_id)
        if winner is not None:
            out[unit["key"]] = winner["name"]
    return out


def set_user_assignment(db, unit_key: str, item_name: str | None, bucket: str | None = None) -> None:
    """사람이 단위의 분류를 정한다. item_name이 None이면 분장 밖으로 둔다. 재분류가 덮어쓰지 않는다."""
    bucket = bucket or (ITEM if item_name else OUTSIDE)
    with db.transaction():
        db.con.execute(
            "INSERT INTO unit_assignments(unit_key, item_name, bucket, confidence, basis, decided_by) "
            "VALUES (?,?,?, 'high', ?, 'user') ON CONFLICT(unit_key) DO UPDATE SET item_name=excluded.item_name, "
            "bucket=excluded.bucket, confidence='high', basis=excluded.basis, decided_by='user', part='', "
            "updated_at=datetime('now')",
            (unit_key, item_name if bucket == ITEM else None, bucket, BASIS_USER))


def assignments(db) -> dict[str, Assignment]:
    return {r["unit_key"]: Assignment(r["unit_key"], r["item_name"], r["bucket"], r["confidence"], r["basis"],
                                      r["evidence"], r["decided_by"], r["part"])
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


# ── 확인 화면이 쓰는 것 ─────────────────────────────────────────────

@dataclass(slots=True)
class ReviewRow:
    unit_key: str
    label: str
    kind: str
    doc_count: int
    bucket: str
    item_name: str | None
    confidence: str
    basis: str
    evidence: str
    decided_by: str
    folder: str
    sample: str


_ATTENTION_ORDER = {UNKNOWN: 0, OUTSIDE: 1, ITEM: 2}


def review_rows(db, attention_only: bool = True) -> list[ReviewRow]:
    """사람이 볼 단위. 기본은 판단 불가·분장 밖·확신 낮음·AI 판단만(분명한 것은 뺀다)."""
    mine = assignments(db)
    rows: list[ReviewRow] = []
    for unit in units.load(db):
        a = mine.get(unit["key"])
        if a is None:
            continue
        needs = (a.bucket != ITEM or a.confidence == "low" or a.basis == BASIS_AI) and a.decided_by != "user"
        if attention_only and not needs:
            continue
        card = unit["card"]
        titles = card.get("titles") or card.get("attachments") or ([card["title"]] if card.get("title") else [])
        rows.append(ReviewRow(unit["key"], unit["label"], unit["kind"], len(unit["doc_ids"]), a.bucket,
                              a.item_name, a.confidence, a.basis, a.evidence, a.decided_by,
                              card.get("folder", ""), " / ".join(titles[:3])))
    rows.sort(key=lambda r: (_ATTENTION_ORDER[r.bucket], r.confidence != "low", -r.doc_count, r.label))
    return rows


def summary_by_item(db) -> list[dict]:
    """분장 항목마다 분류된 단위·문서 수와, 사람이 정한 것 · AI가 판단한 것의 수. 자료가 없는 항목도 나온다."""
    mine = assignments(db)
    out: dict[str, dict] = {o["name"]: {"name": o["name"], "units": 0, "docs": 0, "user": 0, "ai": 0}
                            for o in charter_match.charter_options(db)}
    extra = {OUTSIDE: {"name": OUTSIDE_NAME, "units": 0, "docs": 0, "user": 0, "ai": 0},
             UNKNOWN: {"name": UNKNOWN_NAME, "units": 0, "docs": 0, "user": 0, "ai": 0}}
    for unit in units.load(db):
        a = mine.get(unit["key"])
        if a is None:
            continue
        row = out.get(a.item_name) if a.bucket == ITEM else extra[a.bucket]
        if row is None:
            continue
        row["units"] += 1
        row["docs"] += len(unit["doc_ids"])
        row["user" if a.decided_by == "user" else "ai"] += 1
    return list(out.values()) + [extra[OUTSIDE], extra[UNKNOWN]]
