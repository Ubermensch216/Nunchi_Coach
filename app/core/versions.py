"""판 묶음 — 같은 문서를 여러 번 고쳐 저장한 것들 (행동형 업무 비서 계획서 M3).

'계획(안)', '계획_수정', '계획_최종', '계획_최종_진짜'가 한 폴더에 있는 것이 현장이다.
연도별로 견줄 때 이 넷을 서로 다른 문서 넷으로 세면 안 되고, 그렇다고 이름에
'최종'이 붙은 것을 확정본이라고 믿어서도 안 된다.

묶는 조건 (셋 다)
  · 같은 업무에 묶여 있다
  · 판 표시를 걷어 낸 이름이 같고, 같은 해의 문서다
  · 본문이 충분히 겹친다 — 이름만 같고 내용이 다른 문서는 묶지 않는다

지키는 것
  · **'최종'은 단서일 뿐이다.** 어느 것이 최신본인지는 사유와 확신을 함께 적는
    '후보'다. 단서가 엇갈리면(날짜가 가장 늦은 것과 '최종' 표시가 다른 파일이면,
    '최종'이 둘 이상이면) 후보를 세우지 않는다.
  · 온나라에 등록된 것이 묶음 안에 있으면 그것이 후보에서 앞선다. 등록됐다는 것은
    출처 정보이지 현재 효력을 뜻하지 않는다(core/provenance.py).
  · 내용이 완전히 같은 사본은 판이 아니다. 해시가 하나뿐인 묶음은 만들지 않는다.
  · 사람이 정한 묶음(decided_by='user')은 건드리지 않는다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from . import official as official_docs, provenance

# 이름에서 걷어 낼 판 표시. 4자리 연도는 걷지 않는다 — 해가 다르면 다른 문서다.
_MARKERS = re.compile(
    r"진짜|최최종|최종본?|수정본?|초안|\(안\)|확정본?|송부용|복사본|사본|"
    r"(?<![A-Za-z])(?:final|copy|rev\s*\d*|ver\s*\.?\s*\d+(?:\.\d+)*|v\s*\.?\s*\d+(?:\.\d+)*)(?![A-Za-z])|"
    r"\(\d{1,2}\)|(?<!\d)\d{6}(?!\d)|(?<!\d)\d{8}(?!\d)|"
    # 실제 폴더에서 본 꼬리표: '_결재전'·'(결재)', 날짜 '_0107'·'-2024.05.22.', 사본 '-1'·끝의 '_'
    r"결재\s*전|결재|[_\-](?:0[1-9]|1[0-2])[0-3]\d(?!\d)|[_\-]\d{1,2}$|[_\-]+$",
    re.IGNORECASE)
_DATE_TAIL = re.compile(r"[_\-]((?:0[1-9]|1[0-2])[0-3]\d)(?!\d)")
_ASIDE_FOLDER = re.compile(r"^(?:old|bak|backup|백업|이전|구버전)$|[_\s]old$", re.IGNORECASE)
_FINAL = re.compile(r"최종|확정|(?<![A-Za-z])final(?![A-Za-z])|송부", re.IGNORECASE)
_JUNK = re.compile(r"[\s_\-.·()\[\]]+")

MIN_OVERLAP = 0.5        # 본문 낱말이 이만큼 겹쳐야 같은 문서의 판으로 본다
BODY_CHARS = 3000
_WORD = re.compile(r"[가-힣A-Za-z]{2,}|\d+")


def skeleton(filename: str) -> str:
    """판 표시와 구분 기호를 걷어 낸 이름."""
    stem = _MARKERS.sub(" ", Path(filename).stem)
    return _JUNK.sub("", stem).lower()


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text[:BODY_CHARS]))


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def rebuild(db, task_id: int | None = None) -> int:
    """업무마다 판 묶음을 다시 만든다. 만든 묶음 수를 돌려준다."""
    task_ids = [task_id] if task_id is not None else [row["id"] for row in db.tasks()]
    made = 0
    with db.transaction():
        for tid in task_ids:
            made += _rebuild_task(db, tid)
    return made


def _rebuild_task(db, task_id: int) -> int:
    rows = db.con.execute(
        "SELECT d.id, d.filename, d.path, d.hash, d.eff_year, d.eff_date, d.eff_date_kind, d.fs_mtime, "
        f"       substr(COALESCE(i.body, ''), 1, {BODY_CHARS}) AS idx_body "
        "FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "LEFT JOIN document_index i ON i.doc_id = d.id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL AND d.hash IS NOT NULL", (task_id,)
    ).fetchall()
    ids = [row["id"] for row in rows]
    _clear(db, ids)
    taken = {row["doc_id"] for row in db.con.execute(
        "SELECT m.doc_id FROM version_members m JOIN version_groups g ON g.id = m.group_id "
        "WHERE g.decided_by = 'user'")}

    # document_index가 비어 있는 문서(테스트나 일부 조각만 있는 문서)는 chunks에서 앞부분을 보충한다.
    body_map: dict[int, str] = {}
    missing_body_ids = [row["id"] for row in rows if not row["idx_body"]]
    if missing_body_ids:
        marks = ", ".join("?" * len(missing_body_ids))
        for crow in db.con.execute(
            f"SELECT doc_id, text FROM chunks WHERE doc_id IN ({marks}) ORDER BY doc_id, ordinal",
            missing_body_ids,
        ):
            cur = body_map.get(crow["doc_id"], "")
            if len(cur) < BODY_CHARS:
                body_map[crow["doc_id"]] = (cur + " " + (crow["text"] or "")).strip()[:BODY_CHARS]

    enriched = []
    for row in rows:
        d = dict(row)
        d["body"] = row["idx_body"] or body_map.get(row["id"], "")
        enriched.append(d)

    official = provenance.official_sources(db, ids)
    made = 0
    # P5: 같은 해의 문서 가운데 온나라 등록 공문과 PC 로컬 초안이 공문 제목 알맹이나 본문 겹침(>= MIN_OVERLAP)으로
    # 이어지면, 로컬 초안들만 먼저 묶지 않고 온나라 등록본까지 한 판 묶음으로 합친다.
    remaining = [row for row in enriched if row["id"] not in taken]
    off_rows = [row for row in remaining if row["id"] in official]
    loc_rows = [row for row in remaining if row["id"] not in official]
    grouped_ids: set[int] = set()
    for off_row in off_rows:
        if off_row["id"] in grouped_ids:
            continue
        off_meta = official[off_row["id"]]
        off_year = off_row["eff_year"]
        if off_year is None and off_meta.get("authored_at") and re.match(r"^\d{4}", off_meta["authored_at"]):
            off_year = int(off_meta["authored_at"][:4])
        off_core = official_docs.title_core(off_meta.get("title") or off_row["filename"])
        off_skel = skeleton(off_row["filename"])
        off_words = _words(off_row["body"])
        matched_group = [off_row]
        for loc_row in loc_rows:
            if loc_row["id"] in grouped_ids:
                continue
            if off_year and loc_row["eff_year"] and off_year != loc_row["eff_year"]:
                continue
            loc_words = _words(loc_row["body"])
            overlap = _overlap(off_words, loc_words)
            loc_stem = skeleton(loc_row["filename"])
            title_hit = bool((off_core and off_core in loc_stem) or (off_skel and off_skel == loc_stem))
            if overlap >= MIN_OVERLAP or (title_hit and overlap >= 0.25):
                matched_group.append(loc_row)
        if len({row["hash"] for row in matched_group}) >= 2:
            _store(db, matched_group, official)
            grouped_ids.update(row["id"] for row in matched_group)
            made += 1

    by_name: dict[tuple[str, int | None], list] = defaultdict(list)
    for row in remaining:
        if row["id"] in grouped_ids:
            continue
        key = skeleton(row["filename"])
        if key:
            by_name[(key, row["eff_year"])].append(row)

    for (_name, _year), members in by_name.items():
        for group in _by_content(members):
            if len({row["hash"] for row in group}) < 2:
                continue
            _store(db, group, official)
            made += 1
    return made


def _by_content(members: list) -> list[list]:
    """이름이 같은 것들을 본문이 겹치는 것끼리 다시 가른다."""
    groups: list[tuple[set[str], list]] = []
    for row in members:
        words = _words(row["body"])
        for seed, group in groups:
            if _overlap(seed, words) >= MIN_OVERLAP:
                group.append(row)
                break
        else:
            groups.append((words, [row]))
    return [group for _seed, group in groups if len(group) > 1]


def _when(row) -> str:
    """판의 앞뒤를 가리는 시각. 본문·이름에서 읽은 날짜가 있으면 그것, 없으면 수정 시각."""
    if row["eff_date"] and row["eff_date_kind"] != "fs":
        return f"{row['eff_date']}|{row['fs_mtime'] or ''}"
    return f"|{row['fs_mtime'] or ''}"


def _set_aside(row) -> bool:
    """전임자가 old·bak 폴더로 치워 둔 판인가."""
    return bool(_ASIDE_FOLDER.search(Path(row["path"] or "").parent.name.strip("[] ")))


def _date_tail(row) -> str:
    found = _DATE_TAIL.search(Path(row["filename"]).stem)
    return found.group(1) if found else ""


def _store(db, group: list, official: dict) -> None:
    # 치워 둔 판이 먼저(오래된 것), 그다음 이름의 날짜 꼬리표, 그다음 시각, 온나라 등록본은 맨 나중(확정 판).
    ordered = sorted(group, key=lambda row: (
        not _set_aside(row), row["id"] in official, _date_tail(row), _when(row), row["id"]
    ))
    latest, reason, confidence = _candidate(ordered, official)
    head_row = next((r for r in ordered if r["id"] == latest), ordered[-1])
    label = (official.get(head_row["id"], {}).get("title") or Path(head_row["filename"]).stem)[:80]
    group_id = db.con.execute(
        "INSERT INTO version_groups(label, latest_doc_id, latest_reason, confidence) "
        "VALUES (?, ?, ?, ?)",
        (label, latest, reason, confidence)).lastrowid
    for ordinal, row in enumerate(ordered):
        note = []
        if _FINAL.search(row["filename"]):
            note.append("이름에 최종·확정 표시")
        if row["id"] in official:
            note.append("온나라 등록본")
        if _set_aside(row):
            note.append("old·bak 폴더에 치워 둔 판")
        db.con.execute(
            "INSERT OR REPLACE INTO version_members(group_id, doc_id, ordinal, reason) "
            "VALUES (?, ?, ?, ?)", (group_id, row["id"], ordinal, " · ".join(note) or None))


def _candidate(ordered: list, official: dict) -> tuple[int | None, str, str]:
    """(최신본 후보, 사유, 확신). 단서가 엇갈리면 후보를 세우지 않는다."""
    registered = [row for row in ordered if row["id"] in official]
    if len({row["hash"] for row in registered}) == 1:
        return registered[-1]["id"], "온나라에 등록된 판", "high"
    if registered:
        return None, "온나라에 등록된 판이 둘 이상입니다. 어느 것이 최신인지 확인이 필요합니다.", "low"

    newest = ordered[-1]
    marked = [row for row in ordered if _FINAL.search(row["filename"])]
    if len({row["hash"] for row in marked}) > 1:
        return None, "이름에 최종·확정 표시가 붙은 판이 둘 이상입니다.", "low"
    if marked and marked[-1]["hash"] != newest["hash"]:
        return None, "최종 표시가 붙은 판보다 나중에 고친 판이 있습니다.", "low"
    if marked:
        return newest["id"], "최종 표시가 있고 가장 나중에 고친 판", "medium"
    # 전임자가 스스로 남긴 정리의 흔적은 '최종' 표시만큼의 단서다.
    kept = [row for row in ordered if not _set_aside(row)]
    if len(kept) < len(ordered) and len({row["hash"] for row in kept}) == 1:
        return kept[-1]["id"], "나머지 판은 old·bak 폴더에 치워져 있습니다", "medium"
    tails = [_date_tail(row) for row in ordered]
    if all(tails) and len(set(tails)) == len(tails):
        return newest["id"], f"이름의 날짜 꼬리표가 가장 늦은 판({tails[-1][:2]}.{tails[-1][2:]}.)", "medium"
    return newest["id"], "가장 나중에 고친 판", "low"


def _clear(db, doc_ids: list[int]) -> None:
    """이 문서들이 든, 자동으로 만든 묶음을 지운다."""
    if not doc_ids:
        return
    marks = ", ".join("?" * len(doc_ids))
    db.con.execute(
        f"DELETE FROM version_groups WHERE decided_by = 'ai' AND id IN "
        f"(SELECT group_id FROM version_members WHERE doc_id IN ({marks}))", doc_ids)


def groups_for(db, doc_ids) -> list[dict]:
    """이 문서들이 든 묶음. 묶음마다 구성원을 앞 판부터 싣는다."""
    ids = [int(i) for i in doc_ids]
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    out = []
    for group in db.con.execute(
            f"SELECT DISTINCT g.* FROM version_groups g JOIN version_members m ON m.group_id = g.id "
            f"WHERE m.doc_id IN ({marks}) ORDER BY g.id", ids).fetchall():
        members = db.con.execute(
            "SELECT m.doc_id, m.ordinal, m.reason, d.filename, d.hash FROM version_members m "
            "JOIN documents d ON d.id = m.doc_id WHERE m.group_id = ? ORDER BY m.ordinal",
            (group["id"],)).fetchall()
        out.append({**dict(group), "members": [dict(row) for row in members]})
    return out


def representatives(db, doc_ids) -> dict[int, int]:
    """문서 → 그 묶음을 대표할 문서. 후보가 서 있으면 후보, 없으면 가장 나중 판.

    견줄 때 한 묶음을 한 문서로 세기 위한 것이다. 후보가 없는 묶음은 '확인 필요'로
    따로 알려야 한다 — 그 판단은 부르는 쪽이 groups_for의 confidence를 보고 한다.
    """
    mapping: dict[int, int] = {}
    for group in groups_for(db, doc_ids):
        head = group["latest_doc_id"] or group["members"][-1]["doc_id"]
        for member in group["members"]:
            mapping[member["doc_id"]] = head
    return mapping
