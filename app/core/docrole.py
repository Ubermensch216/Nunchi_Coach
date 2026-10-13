"""문서 역할 — 이 문서가 그 업무에서 무엇이었나 (행동형 업무 비서 계획서 M1).

같은 업무의 문서라도 시행문·평가지표·실적·증빙·제출본·결과 통보는 쓰임이 다르다.
연도별로 견주려면 '2024년의 지표'와 '2025년의 지표'를 집어낼 수 있어야 하고,
그러려면 먼저 어느 문서가 지표인지 알아야 한다.

규칙으로만 정한다. 문서마다 모델을 부르지 않는다(core/labeling.py와 같은 원칙).

지키는 것
  · **못 정하면 정하지 않는다.** 단서가 없으면 other·낮음이다. 억지로 고른 역할은
    그 위의 비교를 조용히 틀리게 만든다.
  · 무엇을 보고 정했는지(basis)를 함께 남긴다.
  · 사람이 고친 역할은 다시 분석해도 덮지 않는다.
  · 온나라 등록본이라는 사실은 확신을 올릴 뿐, 역할을 정하지 않는다.

순서가 우선순위다. "평가 결과 통보"는 통보(시행문)이기 전에 결과이고, "실적 제출
요청"은 실적이기 전에 요청(시행문)이다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import folders, provenance

DIRECTIVE, CRITERIA, PLAN, PERFORMANCE = "directive", "criteria", "plan", "performance"
EVIDENCE, SUBMISSION, RESULT, FORM, OTHER = "evidence", "submission", "result", "form", "other"
# 실제 전임자 자료에서 절반 넘는 문서가 '정하지 못함'이었다(648건 중 361건). 추천서,
# 의결서, 회의록, 시나리오, 견적서, 보도자료, 그리고 전임자가 남긴 메모가 모두 거기
# 들어갔다. 아래 갈래는 그 문서들을 가른다. 앞의 아홉 갈래가 먼저 이긴다 —
# 연도별 비교가 기대는 판정(시행문·지표·서식·실적)은 바뀌지 않는다.
MEMO, DECISION, MEETING, EXPENSE = "memo", "decision", "meeting", "expense"
PUBLICITY, LEGAL, RECEIVED, REFERENCE = "publicity", "legal", "received", "reference"
# 위원 명단, 담당자 명단, 연락망. 누구에게 연락하는지가 여기 있다. 개인정보가 들어 있어
# 내용을 항목으로 옮기지 않고 문서만 가리킨다.
ROSTER = "roster"
ROLES = (DIRECTIVE, CRITERIA, PLAN, PERFORMANCE, EVIDENCE, SUBMISSION, RESULT, FORM,
         MEMO, DECISION, MEETING, EXPENSE, PUBLICITY, LEGAL, RECEIVED, REFERENCE, ROSTER, OTHER)

ROLE_LABEL = {
    DIRECTIVE: "시행문·요청", CRITERIA: "평가지표·기준", PLAN: "계획", PERFORMANCE: "실적 자료",
    EVIDENCE: "증빙", SUBMISSION: "제출본", RESULT: "결과", FORM: "서식",
    MEMO: "전임자 메모", DECISION: "심사·의결", MEETING: "회의·행사 준비", EXPENSE: "지출",
    PUBLICITY: "홍보물", LEGAL: "법령·규정", RECEIVED: "받은 자료", REFERENCE: "참고 자료",
    ROSTER: "명단·연락망",
    OTHER: "정하지 못함",
}
# 업무의 기한·금액·담당자를 읽어 낼 문서가 아닌 갈래. 남이 만든 자료이거나
# 결과물이라, 거기 적힌 금액과 날짜는 이 업무의 것이 아니다.
OUTSIDE_ROLES = frozenset({RECEIVED, REFERENCE, PUBLICITY})
# 규칙이 바뀌면 올린다. 파이프라인이 규칙으로 정한 갈래를 다시 정한다.
VERSION = "5"

# (역할, 이름에서 찾는 꼴). 앞의 것이 이긴다.
_NAME_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (RESULT, re.compile(r"(?:평가|심사|선정|점검)\s*결과|결과\s*(?:통보|알림|보고|발표)")),
    (DIRECTIVE, re.compile(r"요청|알림|통보|안내|협조|공문|시행문")),
    (CRITERIA, re.compile(r"지표|평가\s*기준|심사\s*기준|배점|매뉴얼|편람|작성\s*요령")),
    (FORM, re.compile(r"서식|양식|별지|작성\s*예시")),
    (SUBMISSION, re.compile(r"제출본|제출\s*자료|제출용|최종\s*제출|송부")),
    (EVIDENCE, re.compile(r"증빙|증거\s*자료|근거\s*자료")),
    (PERFORMANCE, re.compile(r"실적|성과|취합|추진\s*현황")),
    (PLAN, re.compile(r"계획|추진\s*방안")),
    # '메모'라는 낱말만으로는 정하지 않는다 — 회의 메모처럼 잡다한 글에도 붙는 이름이다.
    (MEMO, re.compile(r"주의\s*사항|유의\s*사항|체크\s*리스트|쪽지|행사\s*준비|준비\s*사항|인수\s*인계")),
    (DECISION, re.compile(r"의결서|심의\s*의결|심사\s*의결|평가표|심사표|총괄표|회의록")),
    # 현수막 견적서는 행사 준비물이기 전에 지출이다.
    (EXPENSE, re.compile(r"견적|지출|수령부|지급|계산서|청구서|영수증|수당")),
    (MEETING, re.compile(r"시나리오|명패|좌석\s*배치|참석자|식순|현수막|대기\s*화면|발표\s*자료")),
    # '참석자 명단'은 위에서 회의 준비물로 걸린다. 여기 오는 것은 위원·담당자 명단이다.
    (ROSTER, re.compile(r"명단|명부|연락망|비상\s*연락|주소록")),
    (PUBLICITY, re.compile(r"보도\s*자료|카드\s*뉴스|웹툰|포스터|홍보")),
    # '지방공무원법_제75조의2', '행정기본법_제4조' — 조문을 따로 떠 둔 파일
    (LEGAL, re.compile(r"조례|규칙|운영\s*규정|시행령|법률|지침|제\s*\d+\s*조|(?:기본|공무원)법")),
)
# 폴더가 알려 주는 성격(core/folders.py). 이름에 단서가 없을 때만 쓴다.
_FOLDER_ROLES = {"received": RECEIVED, "reference": REFERENCE}

# 본문 앞머리의 꼴. 이름에 단서가 없을 때만 쓰고, 확신은 낮다.
_LETTER_HEAD = re.compile(r"^\s*수신\s*[:：]?\s*\S", re.MULTILINE)
_LETTER_TAIL = re.compile(r"붙임|끝\.|시행\s")
_CRITERIA_HEADER = re.compile(r"배점|평가\s*지표|세부\s*지표")

_SEPARATORS = re.compile(r"[_\-.]+")

HEAD_SECTIONS = 6
HEAD_CHARS = 1500


@dataclass(frozen=True)
class Role:
    role: str
    basis: str
    confidence: str      # high | medium | low


def classify(filename: str, title: str = "", head: str = "", table_headers: str = "",
             official: bool = False, modifiers: tuple[str, ...] = ()) -> Role:
    """이름·제목·본문 앞머리·표 머리글에서 역할을 읽는다.

    modifiers는 이 문서가 놓인 폴더의 성격이다('부서제출' 폴더 → received).
    """
    # 파일 이름은 낱말을 밑줄·붙임표로 잇는다. '결과_통보'도 '결과 통보'다.
    name = _SEPARATORS.sub(" ", f"{Path(filename).stem} {title or ''}")
    for role, pattern in _NAME_RULES:
        found = pattern.search(name)
        if not found:
            continue
        basis = f"이름의 '{found.group(0).strip()}'"
        agrees = ((role == DIRECTIVE and _looks_like_letter(head))
                  or (role == CRITERIA and bool(_CRITERIA_HEADER.search(table_headers))))
        if agrees:
            basis += " · 본문의 꼴이 맞음"
        if official:
            basis += " · 온나라 등록본"
        return Role(role, basis, "high" if agrees or official else "medium")

    if _CRITERIA_HEADER.search(table_headers):
        return Role(CRITERIA, "표 머리글에 지표·배점", "low")
    if _looks_like_letter(head):
        return Role(DIRECTIVE, "본문이 공문 꼴(수신·붙임)", "low")
    for modifier in modifiers:
        if modifier in _FOLDER_ROLES:
            return Role(_FOLDER_ROLES[modifier], f"'{folders.MODIFIER_LABEL[modifier]}' 폴더", "medium")
    return Role(OTHER, "", "low")


def path_modifiers(path: str, root: str) -> tuple[str, ...]:
    """자료 폴더 아래의 폴더 이름들이 말하는 성격(old·reference·received …)."""
    names = folders.relative_parts(path, root)
    return tuple(dict.fromkeys(
        part.modifier for part in map(folders.parse_part, names) if part.modifier))


def reset_outdated(db) -> bool:
    """갈래 규칙이 바뀌었으면 규칙으로 정한 갈래를 지워 다시 정하게 한다.

    사람이 정한 갈래는 그대로 둔다.
    """
    if db.get_meta("docrole_version") == VERSION:
        return False
    with db.transaction():
        db.con.execute("DELETE FROM doc_roles WHERE decided_by != 'user'")
        db.set_meta("docrole_version", VERSION)
    return True


def _looks_like_letter(head: str) -> bool:
    return bool(_LETTER_HEAD.search(head) and _LETTER_TAIL.search(head))


def pending(db, limit: int | None = None) -> list[int]:
    """읽기는 끝났는데 아직 역할을 정하지 않은 문서."""
    return [row["id"] for row in db.con.execute(
        "SELECT d.id FROM documents d JOIN sources s ON s.id = d.source_id AND s.kind != 'upload' "
        "WHERE d.parse_status IN ('ok', 'partial') AND d.missing_since IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM doc_roles r WHERE r.doc_id = d.id) ORDER BY d.id"
        + (f" LIMIT {int(limit)}" if limit else ""))]


def classify_documents(db, doc_ids=None) -> dict[str, int]:
    """문서의 역할을 정해 저장한다. 역할별 건수를 돌려준다.

    사람이 고친 것(decided_by='user')은 건드리지 않는다.
    """
    if doc_ids is None:
        rows = db.con.execute(
            "SELECT d.id, d.filename, d.doc_title, d.path, s.path AS root FROM documents d "
            "JOIN sources s ON s.id = d.source_id AND s.kind != 'upload' "
            "WHERE d.parse_status IN ('ok', 'partial') AND d.missing_since IS NULL").fetchall()
    else:
        ids = [int(i) for i in doc_ids]
        rows = db.con.execute(
            "SELECT d.id, d.filename, d.doc_title, d.path, s.path AS root FROM documents d "
            f"LEFT JOIN sources s ON s.id = d.source_id WHERE d.id IN ({', '.join('?' * len(ids))})",
            ids).fetchall() if ids else []
    kept = {row["doc_id"] for row in db.con.execute(
        "SELECT doc_id FROM doc_roles WHERE decided_by = 'user'")}
    official = provenance.official_sources(db, [row["id"] for row in rows])
    counts: dict[str, int] = {}
    with db.transaction():
        for row in rows:
            if row["id"] in kept:
                continue
            head, headers = _head(db, row["id"])
            found = classify(row["filename"], row["doc_title"] or "", head, headers,
                             official=row["id"] in official,
                             modifiers=path_modifiers(row["path"] or "", row["root"] or ""))
            db.con.execute(
                "INSERT INTO doc_roles(doc_id, role, basis, confidence, decided_by) "
                "VALUES (?, ?, ?, ?, 'ai') ON CONFLICT(doc_id) DO UPDATE SET "
                "role = excluded.role, basis = excluded.basis, confidence = excluded.confidence, "
                "updated_at = datetime('now')",
                (row["id"], found.role, found.basis, found.confidence))
            counts[found.role] = counts.get(found.role, 0) + 1
    return counts


def _head(db, doc_id: int) -> tuple[str, str]:
    """(본문 앞머리, 표 머리글들)."""
    head, headers = [], []
    for section in db.con.execute(
            "SELECT text, meta FROM document_sections WHERE doc_id = ? ORDER BY ordinal", (doc_id,)):
        if len(head) < HEAD_SECTIONS:
            head.append(section["text"])
        header = json.loads(section["meta"] or "{}").get("header")
        if header and header not in headers:
            headers.append(header)
    return "\n".join(head)[:HEAD_CHARS], " | ".join(headers)


def set_role(db, doc_id: int, role: str) -> None:
    """사람이 역할을 정한다. 다시 분석해도 그대로 남는다."""
    if role not in ROLES:
        raise ValueError(f"없는 역할입니다: {role}")
    before = db.con.execute("SELECT role FROM doc_roles WHERE doc_id = ?", (doc_id,)).fetchone()
    db.con.execute(
        "INSERT INTO doc_roles(doc_id, role, basis, confidence, decided_by) "
        "VALUES (?, ?, '담당자가 정함', 'high', 'user') ON CONFLICT(doc_id) DO UPDATE SET "
        "role = excluded.role, basis = excluded.basis, confidence = 'high', decided_by = 'user', "
        "updated_at = datetime('now')", (doc_id, role))
    db.audit("document.role", str(doc_id), detail=before["role"] if before else None, result=role)


def roles(db, doc_ids) -> dict[int, dict]:
    ids = [int(i) for i in doc_ids]
    if not ids:
        return {}
    return {row["doc_id"]: dict(row) for row in db.con.execute(
        f"SELECT * FROM doc_roles WHERE doc_id IN ({', '.join('?' * len(ids))})", ids)}
