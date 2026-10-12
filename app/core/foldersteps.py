"""전임자가 폴더에 적어 둔 순서를 처리 단계로 읽는다.

`1. 선발계획 → 2. 부서제출 → 3. 1차 심사 → … → 9. 시상식`이나
`1. [2023-03-30] 제출 요청 → … → 8. [2023-06-20] 자료 제출`처럼 번호·날짜가 붙은
폴더는 전임자가 직접 정리한 일의 순서다. 문서를 날짜순으로 늘어놓아 순서를
재구성하는 것(core/timeline.py)보다 이것이 앞선다 — 추정이 아니라 기록이기 때문이다.

단계마다 그 폴더의 문서 하나를 근거로 건다. 문서 없는 단계는 만들지 않는다.
달은 폴더의 날짜 → 파일명의 날짜 → (그 해에 고친 파일이면) 파일 날짜 → 본문의 날짜
순으로 정하고, 어느 것도 없으면 비운다.
"""

from __future__ import annotations

from collections import Counter

from . import folders
from .timeline import day_bucket

MIN_STEPS = 2
_SKIP = {folders.REFERENCE, folders.OLD, folders.BACKUP}


def plan(db, task_id: int) -> dict[int, list[dict]]:
    """연도 → 단계 목록. 폴더에 순서가 적힌 해만 들어 있다."""
    rows = db.con.execute(
        "SELECT d.id, d.filename, d.path, d.eff_year, d.eff_month, d.eff_date, d.eff_precision, "
        "       d.eff_date_kind, d.fs_mtime, d.char_count, s.path AS root "
        "FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "JOIN sources s ON s.id = d.source_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL ORDER BY d.id", (task_id,)).fetchall()

    # (회차, 번호, 이름) → 그 단계의 문서들. 경로가 아니라 이름으로 모은다 — 같은 단계
    # 폴더가 압축을 푼 사본으로 한 벌 더 있는 일이 흔하다.
    grouped: dict[tuple, list] = {}
    parts_of: dict[tuple, folders.Part] = {}
    for row in rows:
        names = folders.relative_parts(row["path"], row["root"])
        parsed = [folders.parse_part(name) for name in names]
        at = next((i for i in range(len(parsed) - 1, -1, -1) if parsed[i].is_step), None)
        if at is None or parsed[at].modifier in _SKIP:
            continue
        # 참고·받은 자료 안의 번호 폴더는 남이 정리한 것이고, old 바로 아래의 날짜
        # 폴더는 받은 자료를 날짜별로 쌓아 둔 것이다. 이 일의 순서가 아니다.
        if any(p.modifier in (folders.REFERENCE, folders.RECEIVED, folders.SUBMITTED)
               for p in parsed[:at]):
            continue
        if at and parsed[at - 1].modifier in (folders.OLD, folders.BACKUP) and parsed[at - 1].order is None:
            continue
        # 단계 폴더 아래의 old·bak에 든 것은 그 단계의 이전 판이다. 근거로 삼지 않는다.
        if any(p.modifier in (folders.OLD, folders.BACKUP) for p in parsed[at + 1:]):
            continue
        above = [p for p in parsed[:at] if not p.is_step]
        round_key = folders.Part(
            "", year=next((p.year for p in reversed(above) if p.year), None),
            half=next((p.half for p in reversed(above) if p.half), None),
            session=next((p.session for p in reversed(above) if p.session), None)).round_key
        key = (round_key, parsed[at].order, parsed[at].date, folders.compact(parsed[at].label))
        grouped.setdefault(key, []).append(row)
        parts_of[key] = parsed[at]

    # 한 회차의 단계들은 같은 해의 일이다. 단계 하나의 문서가 우연히 다른 해로 잡혀도
    # (나중에 고친 서식 등) 그 단계만 다른 해로 떨어져 나가지 않게 회차의 해를 쓴다.
    round_year: dict[str, int | None] = {}
    for round_key in {key[0] for key in grouped}:
        round_year[round_key] = _mode(
            row["eff_year"] for key, members in grouped.items()
            if key[0] == round_key and not parts_of[key].date for row in members)

    by_year: dict[int, list[tuple]] = {}
    for key, members in grouped.items():
        part = parts_of[key]
        year = int(part.date[:4]) if part.date else round_year[key[0]]
        if year is None:
            continue
        month, day, hint = _when(part, members, year)
        anchor = max(members, key=lambda r: (r["eff_precision"] == "day", r["char_count"] or 0, -r["id"]))
        label = part.label or part.raw
        by_year.setdefault(year, []).append((key, part, month, day, hint, label, anchor["id"]))

    result: dict[int, list[dict]] = {}
    for year, found in by_year.items():
        if len(found) < MIN_STEPS:
            continue
        # 같은 회차의 단계끼리 묶어, 회차는 가장 이른 때 순으로, 단계는 번호 순으로.
        parents: dict[str, list[tuple]] = {}
        for item in found:
            parents.setdefault(item[0][0], []).append(item)

        def started(parent: str) -> tuple:
            known = [(m, d or 1) for _, _, m, d, *_ in parents[parent] if m]
            return (min(known) if known else (13, 0), parent)

        steps = []
        for parent in sorted(parents, key=started):
            ordered = sorted(parents[parent], key=lambda item: (
                item[1].order if item[1].order is not None else 99,
                item[1].date or "9999", item[0][3]))
            for _key, _part, month, _day, hint, label, doc_id in ordered:
                steps.append({"ordinal": len(steps) + 1, "label": label[:60], "month": month,
                              "day_hint": hint, "doc_id": doc_id, "gap_note": None})
        result[year] = steps
    return result


def _when(part: folders.Part, members, year: int) -> tuple[int | None, int | None, str]:
    if part.date:
        month, day = int(part.date[5:7]), int(part.date[8:10])
        return month, day, f"{month}월 {day_bucket(day)}"
    dated = [r for r in members if r["eff_year"] == year and r["eff_month"]
             and r["eff_date_kind"] != "fs"]
    named = [r for r in dated if r["eff_date_kind"] == "filename"]
    if named:
        return _from_documents(named)
    # 본문 앞머리의 날짜는 그 문서를 쓴 날이 아닐 수 있다(공적 기간, 사업 기간).
    # 번호 폴더 안에서는 그 해에 고친 파일의 날짜가 일한 때에 더 가깝다. 다만 파일
    # 날짜는 복사하면 바뀌므로 그 해의 것일 때만, 그렇다고 밝히고 쓴다.
    stamps = [str(r["fs_mtime"]) for r in members if str(r["fs_mtime"] or "")[:4] == str(year)]
    if stamps:
        month = _mode(int(stamp[5:7]) for stamp in stamps)
        return month, None, f"{month}월(파일 날짜)"
    if dated:
        return _from_documents(dated)
    return None, None, ""


def _from_documents(rows) -> tuple[int, int | None, str]:
    month = _mode(r["eff_month"] for r in rows)
    days = [int(r["eff_date"][8:10]) for r in rows
            if r["eff_month"] == month and r["eff_precision"] == "day"]
    day = min(days) if days else None
    return month, day, f"{month}월 {day_bucket(day)}".strip()


def _mode(values):
    counted = Counter(value for value in values if value is not None)
    return counted.most_common(1)[0][0] if counted else None


def outside_the_work(db, doc_ids) -> set[int]:
    """이 일을 한 기록이 아닌 문서 — 이전 판, 참고자료, 남에게 받은 자료, 홍보 결과물.

    이런 문서의 날짜로 '언제 하는 일'과 '어떤 순서'를 읽으면 남의 일정이 섞인다.
    """
    from . import docrole

    ids = [int(i) for i in doc_ids]
    if not ids:
        return set()
    marks = ", ".join("?" * len(ids))
    aside = {folders.OLD, folders.BACKUP, folders.REFERENCE, folders.RECEIVED}
    out = set()
    for row in db.con.execute(
            "SELECT d.id, d.path, s.path AS root, r.role FROM documents d "
            "LEFT JOIN sources s ON s.id = d.source_id LEFT JOIN doc_roles r ON r.doc_id = d.id "
            f"WHERE d.id IN ({marks})", ids):
        if row["role"] in docrole.OUTSIDE_ROLES:
            out.add(row["id"])
            continue
        # 맨 위의 old 묶음([0. old])은 지난 회차의 기록이다. 그것은 이 일의 선례다.
        # 업무 폴더 안쪽의 old·bak만 이전 판으로 본다.
        parsed = [folders.parse_part(name)
                  for name in folders.relative_parts(row["path"] or "", row["root"] or "")]
        named = next((i for i, part in enumerate(parsed) if not part.transparent), len(parsed))
        found = {part.modifier for part in parsed[named + 1:]} & aside
        # 전임자는 받은 요청 공문과 평가 기준도 '참고' 폴더에 넣어 둔다. 그것은 남의
        # 자료가 아니라 이 일의 기한과 기준이 적힌 문서다.
        if found == {folders.REFERENCE} and row["role"] in (docrole.DIRECTIVE, docrole.CRITERIA):
            continue
        if found:
            out.add(row["id"])
    return out
