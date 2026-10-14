"""근거의 자격 — 질문의 답에 어떤 문서를 먼저 쓰는가 (규제개혁 개선계획서 P1-4, 6.2).

검색 순위는 '질문과 얼마나 닮았나'만 본다. 실제 자료에서 그것만으로는 세 가지가 섞였다.

  · AI가 쓴 전략 보고서와 정기간행물. 질문과 낱말이 잘 겹쳐 순위가 높지만 업무의 의무·기준을
    말할 자격이 없다(R03·R04).
  · 여러 기관 것을 모은 취합표. 전국 건의과제 목록 한 장에 다른 지자체의 행이 수백 개다.
    "우리 과제의 소관부처는?"에 남의 과제 행이 근거 자리를 차지한다(R01·R05).
  · 압축 묶음. 구성원마다 다른 문서인데 한 문서로 잡힌다(P1-2).

이 문서들을 지우지 않는다. 참고 근거로 표시하고, 다른 근거가 있으면 근거 자리를 몇 개까지만
쓰게 한다. 근거 목록에는 무슨 자료인지 밝힌다.

읽지 못한 문서는 '없는 문서'가 아니다. 보호(DRM)·스캔·파싱 실패 문서가 질문과 관련돼 보이면
그렇다고 알린다 — 그 문서에 대해서는 "자료에 없다"고 말할 수 없다(개선계획서 7장).
"""

from __future__ import annotations

from ..core import authority, korean

REFERENCE_SLOTS = 2              # 다른 근거가 있을 때 참고 근거가 차지할 수 있는 자리
_COMPILED_OTHERS = 20            # 다른 기관 행이 이만큼 넘고
_COMPILED_RATIO = 3              # 우리 행의 이 배를 넘으면 여러 기관 취합표다
_MAX_UNREAD = 5

AI, PUBLICATION, ARCHIVE, COMPILED = "ai", "publication", "archive", "compiled"
NOTE = {AI: "참고자료 · AI 생성 표시", PUBLICATION: "참고자료 · 발간물·연구자료",
        ARCHIVE: "압축 묶음 안의 문서", COMPILED: "여러 기관 취합표 — 다른 기관의 행이 섞여 있음"}

UNREAD_STATUS = {"encrypted": "암호·보호 문서", "failed": "읽기 실패", "unsupported": "지원하지 않는 형식",
                 "empty": "글자를 얻지 못함(스캔 등)", "too_large": "크기 상한 초과", "locked": "다른 프로그램이 열고 있음"}


def references(db, doc_ids) -> dict[int, str]:
    """참고 근거로 볼 문서 {문서: 종류}."""
    ids = sorted({int(i) for i in doc_ids})
    if not ids:
        return {}
    out: dict[int, str] = {}
    for doc_id, marker in authority.reference_only(db, ids).items():
        out[doc_id] = AI if marker == authority.AI_GENERATED else PUBLICATION
    marks = ", ".join("?" * len(ids))
    for row in db.con.execute(f"SELECT id FROM documents WHERE id IN ({marks}) AND parser = 'archive'", ids):
        out.setdefault(row["id"], ARCHIVE)
    has_scans = db.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'matter_scans'").fetchone()
    if has_scans:
        for row in db.con.execute(
                f"SELECT doc_id, rows_own, rows_other FROM matter_scans WHERE doc_id IN ({marks}) AND state = 'read'", ids):
            if row["rows_other"] >= _COMPILED_OTHERS and row["rows_other"] > _COMPILED_RATIO * max(row["rows_own"], 1):
                out.setdefault(row["doc_id"], COMPILED)
    return out


def limit(ranked: list[tuple[int, float]], doc_of: dict[int, int], refs: dict[int, str],
          slots: int = REFERENCE_SLOTS) -> list[tuple[int, float]]:
    """참고 근거가 다른 근거를 밀어내지 않게 한다. 다른 근거가 없으면 그대로 둔다."""
    if not any(doc_of.get(cid) not in refs for cid, _ in ranked):
        return ranked
    kept, used = [], 0
    for chunk_id, score in ranked:
        if doc_of.get(chunk_id) in refs:
            if used >= slots:
                continue
            used += 1
        kept.append((chunk_id, score))
    return kept


def note(db, doc_id: int, refs: dict[int, str] | None = None) -> str:
    """근거 한 줄에 붙일 말: '2025년 자료 · 참고자료 · AI 생성 표시'."""
    refs = refs if refs is not None else references(db, [doc_id])
    row = db.con.execute("SELECT eff_year FROM documents WHERE id = ?", (doc_id,)).fetchone()
    parts = [f"{row['eff_year']}년 자료"] if row and row["eff_year"] else []
    if doc_id in refs:
        parts.append(NOTE[refs[doc_id]])
    return " · ".join(parts)


_GENERIC = frozenset({"업무", "자료", "문서", "관련", "내용", "무엇", "어떻게", "언제", "누가", "얼마", "어디",
                      "확인", "알려", "있어", "있나", "해야", "하나", "되나", "올해", "작년", "이번", "지난"})


def unread_related(db, question: str, scope) -> list[dict]:
    """질문과 이름이 겹치는데 본문을 읽지 못한 문서. 그 문서에 대해서는 '없다'고 말할 수 없다."""
    words = [w for w in (korean.stem(t) for t in korean.tokens(question))
             if len(w) >= 2 and w not in _GENERIC and not w.isdigit()]
    if not words:
        return []
    clause, params = scope.predicate("d") if scope is not None else ("d.missing_since IS NULL", [])
    statuses = list(UNREAD_STATUS)
    marks = ", ".join("?" * len(statuses))
    rows = db.con.execute(
        f"SELECT d.id, d.filename, d.path, d.parse_status FROM documents d "
        f"WHERE {clause} AND d.parse_status IN ({marks}) ORDER BY d.id", (*params, *statuses)).fetchall()
    found = []
    for row in rows:
        name = row["filename"] or ""
        if any(word in name for word in words):
            found.append({"doc_id": row["id"], "filename": name, "path": row["path"],
                          "reason": UNREAD_STATUS[row["parse_status"]]})
        if len(found) >= _MAX_UNREAD:
            break
    return found


def unread_line(found: list[dict]) -> str:
    if not found:
        return ""
    listing = ", ".join(f"‘{d['filename']}’({d['reason']})" for d in found)
    return (f"질문과 관련돼 보이지만 본문을 읽지 못한 문서가 있습니다: {listing}. "
            f"이 문서들에 대해서는 '자료에 없다'고 판단할 수 없습니다. 원본을 직접 열어 확인하세요.")


def material_line(db, scope) -> str:
    """질문한 해가 자료의 범위를 벗어났으면, 자료가 어디서 끝나는지와 확인할 곳을 말한다."""
    if scope is None or not scope.years:
        return ""
    clause, params = [], []
    if scope.task_id is not None:
        clause.append("EXISTS (SELECT 1 FROM task_docs td WHERE td.doc_id = d.id AND td.task_id = ?)")
        params.append(scope.task_id)
    if scope.doc_ids is not None:
        clause.append("d.id IN (" + (",".join("?" * len(scope.doc_ids)) or "NULL") + ")")
        params.extend(scope.doc_ids)
    where = " AND ".join(["d.missing_since IS NULL", "d.eff_year IS NOT NULL", *clause])
    row = db.con.execute(f"SELECT MIN(d.eff_year) lo, MAX(d.eff_year) hi FROM documents d WHERE {where}",
                         params).fetchone()
    if not row or row["hi"] is None or min(scope.years) <= row["hi"]:
        return ""
    asked = ", ".join(f"{year}년" for year in sorted(scope.years))
    span = f"{row['lo']}년" if row["lo"] == row["hi"] else f"{row['lo']}~{row['hi']}년"
    return (f"자료는 {span} 것입니다. 질문한 {asked} 회차의 자료는 이 폴더에 없습니다 — "
            f"최신 공문(온나라)과 현 담당 부서·상급기관 담당자에게 확인하세요. 지난 회차가 어떻게 됐는지 "
            f"보려면 연도를 {row['hi']}년으로 바꿔 질문하세요.")
