"""질문 문장에서 규칙으로 뽑아내는 범위(scope) — 연도.

생성 모델에게 맡기지 않는다(원칙 1 — 규칙으로 가능한 것은 LLM에 맡기지
않는다). "작년 예산 요구자료는 누가 만들었어?"에서 연도를 못 뽑으면 모든
연도의 예산 문서가 섞여 근거로 들어온다 — 실측으로 확인된 실패다
(doc/질문화면_RAG_개선계획.md §1 측정 3).

core/dating.py와는 다른 문제를 푼다. dating.py는 문서 본문에서 절대
날짜("2024. 9. 12.")를 찾는다. 문서는 스스로 "작년"이라고 말하지 않는다.
질문은 "오늘"을 기준으로 상대적으로 말하므로, 질의 시각(오늘)이 있어야
해석할 수 있는 별도의 규칙이 필요하다.
"""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

# dating.py의 절대연도 패턴과 같은 접미사 집합을 쓴다("년도"·"학년도" 등
# 공공기관 문서에서 흔한 표기). 다만 여기는 질의 문장을 보므로 별도로 둔다
# — 문서 본문 파싱과 질의 해석은 성격이 다른 문제라 한쪽을 고쳐도 다른
# 쪽이 깨지지 않게 갈라 둔다.
_ABSOLUTE_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*(?:회계연도|년도|년|학년도)(?!\d)")
_LAST_YEAR = re.compile(r"작년|지난\s*해|지난해|전년도?")
_THIS_YEAR = re.compile(r"올해|금년|이번\s*해")
_NEXT_YEAR = re.compile(r"내년|다음\s*해")


@dataclass(slots=True)
class QueryScope:
    """질문의 범위. 문장에서 읽어 내거나(연도) 사용자가 직접 고른다(업무).

    사용자가 고른 것이 문장에서 읽어 낸 것을 이긴다. "작년"이라고 썼는데
    화면에서 2023년을 골랐다면 그 사람은 2023년을 보고 싶은 것이다.
    """

    years: list[int] = field(default_factory=list)
    task_id: int | None = None
    # None은 프로젝트 전체, 빈 목록은 허용된 문서 없음이다.
    doc_ids: list[int] | None = None
    notebook_id: int | None = None
    # 노트북 대화의 방식. ""는 보통 질문, "overview"는 선택한 자료 전체 정리.
    mode: str = ""
    # 노트북 대화에서 현행 법령을 함께 조회할지(설정에서 켠 경우에만 쓰인다).
    law: bool = False
    year_basis: str = "document_date"
    case_id: int | None = None

    def __post_init__(self):
        if self.year_basis not in {"document_date", "performance", "evaluation", "submitted", "case"}:
            raise ValueError("연도 기준이 올바르지 않습니다.")
        if self.year_basis == "case" and self.case_id is None:
            raise ValueError("수행 건 범위에는 case_id가 필요합니다.")

    def fingerprint(self) -> str:
        payload = {"years": sorted(set(self.years)), "task": self.task_id,
                   "documents": None if self.doc_ids is None else sorted(set(self.doc_ids)),
                   "notebook": self.notebook_id, "year_basis": self.year_basis, "case": self.case_id}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @property
    def has_year(self) -> bool:
        return bool(self.years)

    @property
    def has_task(self) -> bool:
        return self.task_id is not None

    def merged_with(self, chosen: "QueryScope | None") -> "QueryScope":
        if chosen is None:
            return self
        return QueryScope(
            years=chosen.years or self.years,
            task_id=chosen.task_id if chosen.task_id is not None else self.task_id,
            doc_ids=chosen.doc_ids if chosen.doc_ids is not None else self.doc_ids,
            notebook_id=chosen.notebook_id,
            mode=chosen.mode,
            law=chosen.law,
            year_basis=chosen.year_basis,
            case_id=chosen.case_id,
        )

    def predicate(self, alias: str = "d") -> tuple[str, list]:
        """문서를 읽는 모든 검색·보조 검사에 같은 범위를 적용한다."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", alias):
            raise ValueError("문서 테이블 별칭이 올바르지 않습니다.")
        clauses, params = [f"{alias}.missing_since IS NULL"], []
        if self.doc_ids is not None:
            clauses.append(f"{alias}.id IN (" + (",".join("?" for _ in self.doc_ids) or "NULL") + ")")
            params.extend(self.doc_ids)
        if self.task_id is not None:
            clauses.append(f"EXISTS (SELECT 1 FROM task_docs scope_td WHERE scope_td.doc_id={alias}.id AND scope_td.task_id=?)")
            params.append(self.task_id)
        if self.year_basis == "case":
            # 수행 건의 명시적 기준 문서만 사용한다. 업무의 모든 자료로 넓히지 않는다.
            clause = f"EXISTS (SELECT 1 FROM work_cases scope_wc WHERE scope_wc.id=? AND scope_wc.basis_doc_id={alias}.id"
            params.append(self.case_id)
            if self.years:
                clause += " AND scope_wc.period_year IN (" + ",".join("?" for _ in self.years) + ")"
                params.extend(self.years)
            clauses.append(clause + ")")
        elif self.years and self.year_basis != "document_date":
            clauses.append(f"EXISTS (SELECT 1 FROM doc_years scope_dy WHERE scope_dy.doc_id={alias}.id AND scope_dy.kind=? AND scope_dy.year IN ("
                           + ",".join("?" for _ in self.years) + "))")
            params.extend([self.year_basis, *self.years])
        elif self.years:
            clauses.append(f"{alias}.eff_year IN (" + ",".join("?" for _ in self.years) + ")")
            params.extend(self.years)
        return " AND ".join(clauses), params

    def search_documents(self, db) -> list[int] | None:
        """업무·연도를 순위 제한 전에 적용한다. 무제한 범위는 None을 유지한다."""
        if not self.has_task and not self.has_year and self.case_id is None:
            return self.doc_ids
        clause, params = self.predicate()
        return [row[0] for row in db.con.execute(f"SELECT d.id FROM documents d WHERE {clause}", params)]


def infer_scope(question: str, today: date | None = None) -> QueryScope:
    """질문 문장을 훑어 연도를 뽑는다. 못 찾으면 빈 범위 — 필터를 걸지 않는다."""
    today = today or date.today()
    years: set[int] = set()

    for match in _ABSOLUTE_YEAR.finditer(question):
        years.add(int(match.group(1)))
    if _LAST_YEAR.search(question):
        years.add(today.year - 1)
    if _THIS_YEAR.search(question):
        years.add(today.year)
    if _NEXT_YEAR.search(question):
        years.add(today.year + 1)

    return QueryScope(years=sorted(years))
