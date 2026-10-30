"""공문 — 업무의 뼈대가 되는 온나라 등록 기록 (공문기준_업무분석_계획서 P3).

폴더는 전임자가 정리한 만큼만 믿을 수 있다. 공문은 기관이 등록한 기록이다. 제목에 업무
이름이 적혀 있고, 단위과제는 기관이 정한 업무 분류이며, 회신 공문은 요청 공문의 문서번호를
인용한다. 그래서 업무 찾기(ai/discover.py)는 공문으로 뼈대를 세운 뒤 PC 파일을 거기에 붙인다.

여기에는 DB를 고치지 않는 것만 둔다 — 제목의 알맹이, 문서번호 인용 찾기, 출처 기록 읽기.

지키는 것
  · **제목에 적힌 것만 쓴다.** 동작어(알림·제출·회신…)는 띄어 쓴 낱말 단위로만 뗀다.
    '실행계획'의 '계획'은 업무 이름의 일부라 남는다.
  · 문서번호는 원문 그대로 대조한다. 비슷한 번호를 같은 번호로 보지 않는다.
  · 이어진 파일이 없는 출처 기록(아직 분석 전)은 읽지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import folders

# 공문 제목 끝에 붙는 동작어. 업무가 아니라 그 공문이 하는 일이다.
ACTION_WORDS = frozenset({
    "알림", "안내", "요청", "제출", "회신", "송부", "통보", "보고", "결과", "결과보고", "계획",
    "개최", "협조", "협조요청", "의뢰", "시행", "실시", "추진", "검토", "관련", "건", "통지",
    "공고", "제출요청", "회신요청", "재요청", "재안내", "변경", "변경알림", "긴급", "정정",
    "에", "대한", "따른", "위한", "및", "등", "의", "내용",
})
# 알맹이가 이보다 짧으면 업무 이름으로 쓰지 않는다('교육'·'회의'는 어디에나 있다).
MIN_CORE = 4


def title_label(title: str) -> str:
    """연도·차수·괄호·꼬리표를 떼고 끝의 동작어를 뗀 제목. 사람이 읽는 업무 이름이다."""
    label = folders.parse_part(title or "").label
    words = re.sub(r"[\"'“”‘’]", " ", label).split()
    while words and words[-1] in ACTION_WORDS:
        words.pop()
    while words and words[0] in ACTION_WORDS:
        words.pop(0)
    return " ".join(words).strip(" -·,")


def title_core(title: str) -> str:
    """같은 업무인지 가리는 열쇠. 짧으면 빈 값 — 그 제목으로는 업무를 세우지 않는다."""
    core = re.sub(r"[^\w]", "", folders.compact(title_label(title)))
    return core if len(core) >= MIN_CORE else ""


def normalize_number(value: str) -> str:
    """대조할 문서번호의 모양. 기관 이름이 앞에 붙은 번호('행정안전부 지방행정정책관-567')는
    부서-번호 부분만 쓴다 — 인용하는 쪽이 기관 이름을 빼고 적는 일이 흔하다."""
    parts = re.sub(r"\s*-\s*", "-", value or "").split()
    return parts[-1] if parts else ""


# 앞은 글자로 시작하는 부서 이름, 뒤는 번호. 밑줄은 경계로 본다(파일 이름 '붙임1_기획과-1234').
_NUMBER = re.compile(r"(?<![^\W_])[^\W\d_][^\W_]{1,30}-\d{1,7}(?!\d)")


def numbers_in(text: str) -> set[str]:
    """글에 적힌 문서번호 모양들. 알려진 번호와 정확히 같은 것만 인용으로 본다 —
    '기획과-12'는 '기획과-1234'도 '교육기획과-12'도 아니다."""
    # 붙임표 앞뒤의 띄어쓰기만 없앤다. 다른 띄어쓰기는 번호의 앞 경계다.
    return set(_NUMBER.findall(re.sub(r"\s*-\s*", "-", text or "")))


@dataclass(slots=True)
class Record:
    """출처 기록 한 건(공문 한 건)과 이어진 문서들."""

    id: int
    title: str
    document_number: str = ""
    authored_at: str | None = None
    department: str = ""
    task_card: str = ""
    body_ids: list[int] = field(default_factory=list)
    attachment_ids: list[int] = field(default_factory=list)
    hashes: set[str] = field(default_factory=set)

    @property
    def doc_ids(self) -> list[int]:
        return self.body_ids + self.attachment_ids

    @property
    def year(self) -> str:
        return (self.authored_at or "")[:4] if re.match(r"\d{4}", self.authored_at or "") else ""


def records(db) -> list[Record]:
    """문서 행에 이어진 파일이 있는 공문. 원본이 사라진 문서는 뺀다."""
    out: dict[int, Record] = {}
    for row in db.con.execute(
            "SELECT r.id, r.title, r.document_number, r.authored_at, r.department, r.task_card,"
            " a.role, a.doc_id, a.sha256 FROM source_records r"
            " JOIN source_assets a ON a.record_id = r.id"
            " JOIN documents d ON d.id = a.doc_id AND d.missing_since IS NULL"
            " WHERE a.status = 'downloaded' ORDER BY r.id, a.id"):
        record = out.setdefault(row["id"], Record(
            row["id"], row["title"] or "", row["document_number"] or "", row["authored_at"],
            row["department"] or "", row["task_card"] or ""))
        (record.body_ids if row["role"] == "body" else record.attachment_ids).append(row["doc_id"])
        if row["sha256"]:
            record.hashes.add(row["sha256"])
    return list(out.values())


def body_text(db, doc_ids: list[int], limit: int = 20000) -> str:
    """본문 문서의 앞부분. 관련 공문의 문서번호는 대개 첫머리('1. 관련: …')에 있다."""
    if not doc_ids:
        return ""
    marks = ", ".join("?" * len(doc_ids))
    text = " ".join(row["text"] for row in db.con.execute(
        f"SELECT text FROM chunks WHERE doc_id IN ({marks}) ORDER BY doc_id, ordinal", doc_ids))
    return text[:limit]


def body_texts(db, records_list: list[Record], limit: int = 20000) -> dict[int, str]:
    """여러 공문의 본문 앞부분을 한 번의 쿼리로 읽는다(N+1 방지)."""
    all_ids = sorted({doc_id for record in records_list for doc_id in record.body_ids})
    if not all_ids:
        return {}
    marks = ", ".join("?" * len(all_ids))
    chunks_by_doc: dict[int, list[str]] = {}
    for row in db.con.execute(
            f"SELECT doc_id, text FROM chunks WHERE doc_id IN ({marks}) ORDER BY doc_id, ordinal", all_ids):
        chunks_by_doc.setdefault(row["doc_id"], []).append(row["text"])
    out: dict[int, str] = {}
    for record in records_list:
        parts = [text for doc_id in record.body_ids for text in chunks_by_doc.get(doc_id, ())]
        out[record.id] = " ".join(parts)[:limit] if parts else ""
    return out
