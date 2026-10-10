"""도구들이 함께 쓰는 것 — 대상 업무의 문서 모으기, 올해 기준 판정, 근거 모양 바꾸기.

여기서 하는 판정은 한 곳에만 둔다. 탐색·비교·서식·체크리스트가 각자 '올해 기준이
확인됐는가'를 따로 판정하면 한 산출물은 공식이라 하고 다른 산출물은 사전조사라
하는 일이 생긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ...core import docrole, docyears, reconstruction as rc, yearcompare as yc
from ..registry import Blocked

OFFICIAL, UNCONFIRMED = "official", "unconfirmed"


def task_name(db, task_id: int) -> str:
    row = db.task(task_id)
    if row is None:
        raise Blocked("업무를 찾을 수 없습니다. 업무가 합쳐지거나 지워졌는지 확인해 주세요.")
    return row["name"]


def evidence_dicts(refs) -> list[dict]:
    return [{"doc_id": ref.doc_id, "locator": ref.locator, "quote": ref.quote} for ref in refs]


def sources_of(db, doc_ids) -> list[dict]:
    """산출물을 만들 때 본 문서의 판."""
    ids = sorted({int(i) for i in doc_ids})
    if not ids:
        return []
    return [{"doc_id": row["id"], "hash": row["hash"]} for row in db.con.execute(
        f"SELECT id, hash FROM documents WHERE id IN ({', '.join('?' * len(ids))}) ORDER BY id", ids)]


def filename(db, doc_id: int) -> str:
    row = db.document(doc_id)
    return row["filename"] if row else f"문서 {doc_id}"


def ref_text(db, ref) -> str:
    """보고서에 싣는 근거 한 줄: 파일 이름 — 위치."""
    where = f" — {ref.locator}" if ref.locator else ""
    return f"{filename(db, ref.doc_id)}{where}"


@dataclass
class Target:
    """대상 연도(올해)의 기준이 확인됐는가."""

    year: int
    basis: str = UNCONFIRMED               # official | unconfirmed
    how: str = ""                          # 무엇을 보고 그렇게 정했는가
    criteria_docs: list[int] = field(default_factory=list)
    directive_docs: list[int] = field(default_factory=list)
    due: date | None = None
    due_source: str = ""                   # official | user | ''
    due_note: str = ""
    refs: list = field(default_factory=list)

    @property
    def official(self) -> bool:
        return self.basis == OFFICIAL


def judge_target(db, case_id: int | None, doc_ids, year: int, mat: "yc.Materials") -> Target:
    """올해 기준 판정.

    공식으로 보는 것은 둘뿐이다 — 담당자가 수행 건에 올해 근거를 정했거나, 업무에
    묶인 문서 가운데 **평가 연도가 올해로 적힌** 시행문·지표가 있거나. 올해 작성했다는
    사실만으로는 올해 기준이라 하지 않는다(올해 쓴 문서가 지난해 실적을 다룰 수 있다).
    기한은 그렇게 확인된 시행문에 적힌 날짜 하나, 또는 담당자가 정한 날짜뿐이다.
    """
    target = Target(year)
    target.criteria_docs = mat.docs(year, docrole.CRITERIA)
    target.directive_docs = mat.docs(year, docrole.DIRECTIVE)

    marks = docyears.years(db, target.criteria_docs + target.directive_docs)
    evaluated = [doc for doc in target.criteria_docs + target.directive_docs
                 if year in marks.get(doc, {}).get(docyears.EVALUATION, [])]
    case = db.case(case_id) if case_id else None
    if case is not None and case["basis_state"] in ("official", "user"):
        target.basis, target.how = OFFICIAL, "수행 건에 담당자가 올해 근거를 정함"
    elif evaluated:
        target.basis, target.how = OFFICIAL, f"평가 연도가 {year}년으로 적힌 시행문·지표 {len(evaluated)}건"
    else:
        target.how = f"평가 연도가 {year}년으로 적힌 시행문·지표가 없음"

    if case is not None and case["due_state"] in ("official", "user") and case["due_date"]:
        target.due = date.fromisoformat(case["due_date"])
        target.due_source = case["due_state"]
        return target
    dues: dict[date, rc.Found] = {}
    for doc_id in (evaluated or []):
        for found in yc._found(db, doc_id):
            if found.kind == rc.DEADLINE and found.when is not None and found.when.year >= year:
                dues.setdefault(found.when, found)
    if len(dues) == 1:
        [(when, found)] = dues.items()
        target.due, target.due_source = when, "official"
        target.refs.append(yc.Ref(found.doc_id, found.locator, found.quote))
    elif len(dues) > 1:
        target.due_note = "올해 시행문의 기한이 서로 다릅니다 — 어느 것이 맞는지 확인이 필요합니다"
    return target
