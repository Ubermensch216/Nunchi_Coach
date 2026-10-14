"""A1·A2 — 자료 찾기와 연도×역할 정리 (계획서 2단계).

찾기
  업무에 묶인 문서를 모두 보고, 거기에 업무명·핵심 낱말로 본문을 훑어 찾은 **후보**를
  더한다. 후보는 업무에 묶여 있지 않다 — 이 업무의 자료인지는 사람이 정한다. 그래서
  비교의 값은 후보에서 읽지 않는다(core/continuity.py와 같은 규칙).

  업무에 묶여 있어도 이 업무를 가르는 낱말이 이름과 본문 어디에도 없는 문서는
  '의심'으로 따로 알린다. 비교에서는 뺀다 — 일반 합동평가 자료가 규제혁신 평가
  자료로 섞이면 지표가 뒤섞인다.

  읽지 못한 문서(암호·권한·형식·손상)는 조용히 빼지 않고 사유와 함께 적는다. 읽을 수
  있는 문서가 하나도 없으면 사용자의 조치가 필요하므로 멈춘다.

정리
  찾은 문서를 연도×역할 격자에 놓는다. 비어 있는 칸(예: 대상 연도의 평가지표)을
  명시한다 — 빈칸이 비교의 결론이 되는 일은 없다.
"""

from __future__ import annotations

import re

from ...core import docrole, yearcompare as yc
from ...search import fts
from .. import policy
from ..registry import REGISTRY, Blocked, Tool
from . import common

# 업무 이름에서 업무를 가르지 못하는 낱말. 이것만 남으면 가를 수 없다고 본다.
GENERIC = frozenset("""
평가 대응 업무 보고 계획 자료 제출 관리 현황 실적 운영 추진 점검 결과 수립 작성 취합 관련
사업 정기 연간 분기 월간 일반 기타 행정 처리 준비 지원 검토 확인 조사 통계 회의 계약 정산
""".split())
CANDIDATE_LIMIT = 40
BODY_CHARS = 3000
NOT_READABLE = ("failed", "encrypted", "unsupported", "empty", "pending")
WHY_UNREADABLE = {
    "failed": "읽지 못함", "encrypted": "암호·보호(DRM)가 걸려 있음", "unsupported": "지원하지 않는 형식",
    "empty": "읽을 내용이 없음", "pending": "아직 읽지 않음",
}


def key_terms(name: str) -> list[str]:
    """업무 이름에서 업무를 가르는 낱말."""
    words = re.split(r"[\s·,/()\-_]+", name)
    return [w for w in words if len(w) >= 2 and w not in GENERIC]


def _mentions(text: str, terms: list[str]) -> bool:
    flat = re.sub(r"[\s_\-]+", "", text)
    return any(term in flat for term in terms)


def run_discover(context, params: dict) -> dict:
    db, task_id = context.db, params["task_id"]
    name = common.task_name(db, task_id)
    terms = key_terms(name)

    assigned = db.con.execute(
        "SELECT d.id, d.filename, d.doc_title, d.parse_status, d.parse_error, d.missing_since, "
        "       substr(COALESCE(i.body, ''), 1, ?) AS body "
        "FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "LEFT JOIN document_index i ON i.doc_id = d.id WHERE td.task_id = ? ORDER BY d.id",
        (BODY_CHARS, task_id)).fetchall()
    unreadable, suspect, included = [], [], []
    for row in assigned:
        if row["missing_since"] is not None:
            unreadable.append({"id": row["id"], "why": "원본 파일이 보이지 않음"})
        elif row["parse_status"] not in ("ok", "partial"):
            unreadable.append({"id": row["id"], "why": WHY_UNREADABLE.get(row["parse_status"], "읽지 못함")})
        elif terms and not _mentions(f"{row['filename']} {row['doc_title'] or ''} {row['body']}", terms):
            suspect.append({"id": row["id"], "why": f"이름과 본문에 '{terms[0]}'이(가) 없음 — 다른 업무의 자료일 수 있음"})
        else:
            included.append({"id": row["id"], "relation": "assigned", "why": "업무에 묶여 있음"})
    if assigned and not included:
        raise Blocked("이 업무의 문서를 읽을 수 없습니다. 자료 폴더의 권한과 파일 상태를 확인한 뒤 "
                      "다시 분석하고 실행해 주세요. (읽지 못한 문서의 사유는 근거 목록에 있습니다.)")
    if not assigned:
        raise Blocked("이 업무에 묶인 문서가 없습니다. 문서 화면에서 업무를 정한 뒤 실행해 주세요.")

    taken = {row["id"] for row in assigned}
    owned_elsewhere = {row["doc_id"] for row in db.con.execute(
        "SELECT doc_id FROM task_docs WHERE task_id != ?", (task_id,))}
    excluded, candidates = [], []
    seen_hash = {row["hash"] for row in db.con.execute(
        "SELECT hash FROM documents WHERE id IN (%s)" % ", ".join(str(i["id"]) for i in included))}
    for term in terms[:3]:
        for doc_id in fts.search_document_ids(db, term, CANDIDATE_LIMIT):
            if doc_id in taken or any(doc_id == c["id"] for c in candidates):
                continue
            row = db.document(doc_id)
            if row is None or row["missing_since"] is not None or row["parse_status"] not in ("ok", "partial"):
                continue
            if doc_id in owned_elsewhere:
                excluded.append({"id": doc_id, "why": "다른 업무에 묶여 있음"})
            elif row["hash"] in seen_hash:
                continue
            else:
                candidates.append({"id": doc_id, "relation": "candidate",
                                   "why": f"업무에 묶여 있지 않지만 '{term}'이(가) 나옴"})
    return {"task_id": task_id, "terms": terms, "included": included, "candidates": candidates,
            "suspect": suspect, "unreadable": unreadable, "excluded": excluded,
            "assigned": len(assigned)}


def run_classify(context, params: dict) -> dict:
    db = context.db
    found = params["inputs"]["discover"]
    doc_ids = [item["id"] for item in found["included"]]
    mat = yc.materials(db, doc_ids)
    case_id = context.case_id
    target = common.judge_target(db, case_id, doc_ids, params["target_year"], mat)

    grid = {str(year): {role: ids for role, ids in roles.items()}
            for year, roles in sorted(mat.by_year.items())}
    wanted = (docrole.DIRECTIVE, docrole.CRITERIA, docrole.FORM, docrole.PERFORMANCE)
    holes = [{"year": year, "role": role}
             for year in sorted(set(mat.by_year) | {target.year})
             for role in wanted if not mat.docs(year, role)]
    return {
        "grid": grid, "years": sorted(mat.by_year), "undated": mat.undated, "holes": holes,
        "uncertain_groups": [group["id"] for group in mat.uncertain_groups],
        "folded": {str(k): v for k, v in mat.folded.items()},
        "target": {"year": target.year, "basis": target.basis, "how": target.how,
                   "criteria_docs": target.criteria_docs, "directive_docs": target.directive_docs,
                   "due": target.due.isoformat() if target.due else None,
                   "due_source": target.due_source, "due_note": target.due_note},
    }


REGISTRY.register(Tool("discover", "1", policy.READ, run_discover, label="관련 자료 찾기"))
REGISTRY.register(Tool("classify", "1", policy.READ, run_classify, label="연도·역할별 정리"))
