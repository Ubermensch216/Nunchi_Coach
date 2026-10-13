"""전임자의 말, 바뀐 것, 조심할 자료 — 사람이 인수인계하며 입으로 전하는 것들.

기한·금액 같은 값이 아니라 문장 그대로가 쓸모인 것들이다. 규칙으로만 가려내고 원문을
고치지 않는다. 모두 '유의·위험' 항목으로 올라가 업무 매뉴얼의 "주의사항·담당자 메모"에
보인다.

  · 전임자 메모       메모로 가려낸 문서(주의사항.txt, 행사 준비 참고)를 줄 단위로 올린다.
                      실제 자료에서 메모는 '먼저 읽을 문서' 맨 위에 있었지만 열두 항 가운데
                      준비물 한 줄만 항목이 됐다.
  · 남긴 과제         메모 가운데 '…칸 마련', '…보완'처럼 하지 못하고 넘긴 일.
  · 점검 표시 문서    표의 칸을 ●·◐·○·?로 채워 둔 문서. 전임자가 스스로 매긴 점검이다.
                      기호의 뜻은 풀지 않고 문서만 가리킨다.
  · 바뀐 것           계획서의 '기존 / 변경' 표와 '※변경' 표시. 그 앞 회차의 선례는
                      그대로 따르면 안 된다는 뜻이다.
  · 취급주의 자료     신분증·통장 사본, 수사·징계 자료, 생년월일·연락처가 든 명단.
                      내용은 읽지 않고 건수와 있는 곳만 알린다.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import PureWindowsPath

from . import casedocs, docrole, foldersteps, workstate as ws
from . import reconstruction as rc

MEMO_PREFIX = "전임자 메모 · "
TODO_PREFIX = "전임자가 남긴 과제 · "
CHANGE_PREFIX = "바뀐 것 · "
MARKED_SLOT = "전임자의 점검 표시가 있는 문서"
CARE_PREFIX = "취급주의 · "

_MAX_LINES = 40             # 메모 한 건에서 올리는 줄 수
_MAX_CHANGES = 12
_MAX_LINE = 300
_ITEM_START = re.compile(r"^(?:\d{1,2}\s*[.)]|[-·○●▷▶※*<\[]|[①-⑳]|={2,}|\d{1,2}:\d{2})")
_UNDONE = re.compile(r"(?:마련|보완|개선|검토|추가|반영)\s*(?:필요|요망|할\s*것|예정)?\s*[.)]?$|해야\s*(?:함|한다|됨)\s*[.)]?$")
# "(기존 2일)"처럼 예전 값이 적힌 것만. "(기존 권익침해 구제제도 한계)"는 변경 표시가 아니다.
_CHANGED_MARK = re.compile(r"※\s*변경|\(\s*기존\s*\d[^)]{0,15}\)|\(\s*신설\s*\)")
_TICKS = re.compile(r"열:\s*[●◐○△▲✓✔?]\s*(?:\([^)|]*\))?\s*(?=\||$)", re.MULTILINE)
_MIN_TICKS = 5

# 부서 이름(민원여권과)은 여권 사본이 아니다.
_PRIVATE_COPY = re.compile(r"신분증|주민등록|통장|여권(?!과)|면허증|가족관계|인감|등본|초본")
# "우수사례"의 "수사"에 걸리지 않게 뒤의 말까지 본다. 업무 이름일 뿐인 "소송 지원"은 뺀다.
_CASE_FILE = re.compile(r"진술서|수사\s*(?:업무|과정|기관|의뢰|결과)|피의|징계\s*(?:의결|처분|요구)"
                        r"|고소|고발|(?<![가-힣])사건|감사\s*처분")
_PERSONAL_TABLE = (re.compile(r"생년월일|주민\s*번호"), re.compile(r"연락처|휴대|주\s*소|계좌"))


def items(db, task_id: int, basis: date | None = None) -> list[dict]:
    """업무 하나의 메모·과제·변경·취급주의를 항목 후보로 돌려준다. 저장하지 않는다."""
    rows = db.con.execute(
        "SELECT d.id, d.filename, d.path, d.eff_year, d.parse_status, r.role "
        "FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "LEFT JOIN doc_roles r ON r.doc_id = d.id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL "
        "  AND (d.hash IS NULL OR d.id = (SELECT MIN(x.id) FROM task_docs tx "
        "       JOIN documents x ON x.id = tx.doc_id "
        "       WHERE tx.task_id = td.task_id AND x.hash = d.hash AND x.missing_since IS NULL)) "
        "ORDER BY d.eff_year DESC, d.id", (task_id,)).fetchall()
    if not rows:
        return []
    read = [row for row in rows if row["parse_status"] in ("ok", "partial")]
    outside = foldersteps.outside_the_work(db, [row["id"] for row in read])
    cases = casedocs.collections(db, [row["id"] for row in read])
    out: list[dict] = []
    out += _memos(db, [row for row in read if row["role"] == docrole.MEMO])
    own = [row for row in read if row["id"] not in outside and row["id"] not in cases
           and row["role"] in (docrole.PLAN, docrole.RESULT, docrole.MEETING, docrole.DECISION)]
    out += _changes(db, own)
    out += _marked(db, [row for row in read if row["id"] not in cases])
    out += _care(db, rows)
    return out


def _sections(db, doc_id: int):
    return db.con.execute(
        "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal", (doc_id,)).fetchall()


def _candidate(slot: str, content: str, evidence: list[tuple[int, str, str]],
               state: str = ws.STATED) -> dict:
    return {"kind": rc.RISK, "slot": slot, "content": content[:_MAX_LINE], "basis_state": state,
            "applicability": ws.PAST, "evidence": evidence[:5]}


def _memo_lines(sections) -> list[tuple[str, str]]:
    """(위치, 줄). 번호·기호로 시작하지 않는 줄은 앞줄에 이어 붙인다."""
    out: list[tuple[str, str]] = []
    for section in sections:
        for raw in (section["text"] or "").splitlines():
            line = " ".join(raw.split())
            if not line:
                continue
            if out and not _ITEM_START.match(line) and len(out[-1][1]) + len(line) < _MAX_LINE:
                out[-1] = (out[-1][0], f"{out[-1][1]} {line}")
            else:
                out.append((section["locator"], line))
    return out


def _memos(db, memos) -> list[dict]:
    out: list[dict] = []
    for row in memos:
        name = PureWindowsPath(row["filename"]).stem[:30]
        todo = 0
        for index, (locator, line) in enumerate(_memo_lines(_sections(db, row["id"]))[:_MAX_LINES], start=1):
            if len(line) < 6:
                continue        # '<행사>' 같은 머리말
            evidence = [(row["id"], locator, line)]
            if _UNDONE.search(line):
                todo += 1
                out.append(_candidate(f"{TODO_PREFIX}{name} · {todo:02d}", line, evidence))
            else:
                out.append(_candidate(f"{MEMO_PREFIX}{name} · {index:02d}", line, evidence))
    return out


def _changes(db, documents) -> list[dict]:
    """'기존 / 변경' 표의 행과 '※변경'이 붙은 줄. 같은 말은 한 번만, 최근 문서부터."""
    seen: set[str] = set()
    out: list[dict] = []
    for row in documents:
        for section in _sections(db, row["id"]):
            lines = [" ".join(line.split()) for line in (section["text"] or "").splitlines()]
            lines = [line for line in lines if line]
            found: list[str] = []
            head = rc._TABLE_HEAD.match(lines[0]) if lines else None
            if head and len(lines) >= 2:
                headers = [part.strip() for part in head.group(1).split(" | ")]
                if "기존" in headers and "변경" in headers:
                    cells = {int(n): v.strip() for n, v in rc._TABLE_CELL.findall(lines[1])}
                    before, after = cells.get(headers.index("기존") + 1), cells.get(headers.index("변경") + 1)
                    names = [cells.get(n, "") for n in range(1, headers.index("기존") + 1)]
                    name = " ".join(dict.fromkeys(part for part in names if part))
                    if before and after and before != after and before != "기존":
                        found.append(f"{name}: {before} → {after}" if name else f"{before} → {after}")
            else:
                found += [line for line in lines if _CHANGED_MARK.search(line) and len(line) >= 8]
            for text in found:
                key = rc._normalize(text)
                if key in seen or len(out) >= _MAX_CHANGES:
                    continue
                seen.add(key)
                year = f"{row['eff_year']}년 · " if row["eff_year"] else ""
                out.append(_candidate(f"{CHANGE_PREFIX}{year}{len(out) + 1:02d}", text,
                                      [(row["id"], section["locator"], " ".join(lines)[:400])]))
    return out


def _marked(db, documents) -> list[dict]:
    ids = [row["id"] for row in documents]
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    ticked = {row["doc_id"] for row in db.con.execute(
        f"SELECT doc_id, body FROM document_index WHERE doc_id IN ({marks})", ids)
        if len(_TICKS.findall(row["body"] or "")) >= _MIN_TICKS}
    found = [row for row in documents if row["id"] in ticked]
    if not found:
        return []
    names = list(dict.fromkeys(row["filename"] for row in found))[:4]
    return [_candidate(MARKED_SLOT, " / ".join(names),
                       [(row["id"], "", row["filename"]) for row in found], state=ws.INFERRED)]


def _care(db, rows) -> list[dict]:
    """내용을 읽지 않고 이름·폴더·표 머리만으로 가린다. 읽지 못한 스캔본과 그림도 센다."""
    groups: dict[str, list] = {}
    body_of: dict[int, str] = {}
    ids = [row["id"] for row in rows if row["parse_status"] in ("ok", "partial")]
    if ids:
        marks = ", ".join("?" * len(ids))
        # 표 머리만 보면 된다. 본문 앞부분으로 충분하다.
        body_of = {row["doc_id"]: row["head"] for row in db.con.execute(
            f"SELECT doc_id, substr(body, 1, 3000) AS head FROM document_index WHERE doc_id IN ({marks})", ids)}
    for row in rows:
        where = f"{row['path'] or ''}"
        if _PRIVATE_COPY.search(where):
            groups.setdefault("신분증·통장 사본", []).append(row)
        elif _CASE_FILE.search(row["filename"] or ""):
            groups.setdefault("수사·소송·징계 자료", []).append(row)
        elif all(pattern.search(body_of.get(row["id"], "")) for pattern in _PERSONAL_TABLE):
            groups.setdefault("생년월일·연락처가 든 문서", []).append(row)
    out = []
    for label, found in groups.items():
        folders_seen = list(dict.fromkeys(PureWindowsPath(row["path"]).parent.name for row in found))
        where = ", ".join(folders_seen[:3]) + (" 등" if len(folders_seen) > 3 else "")
        out.append(_candidate(
            CARE_PREFIX + label,
            f"{len(found)}건 ({where} 폴더). 보존이 필요한지 확인하고, 내보내거나 공유할 때 빼세요.",
            [(row["id"], "", row["filename"]) for row in found], state=ws.INFERRED))
    return out
