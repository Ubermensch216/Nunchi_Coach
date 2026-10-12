"""문서에 적힌 일정·기한을 한 해의 달력으로 모은다 — 일정 화면의 '월별 일정'.

반복 주기(core/timeline.py)는 "이 업무는 6월에 한다"까지 말한다. 후임자가 정작 묻는
것은 "6월 며칠까지 무엇을 하나"이고, 그 답은 계획서와 공문에 적혀 있다
(core/reconstruction.py가 업무 항목의 기한으로 읽어 둔다). 여기서는 그 항목들을
달별로 펴서 늘어놓기만 한다. 새로 읽거나 추정하지 않는다.

이 날짜들은 **지난 회차의 날짜**다. 올해의 날짜가 아니다. 화면은 그렇게 말해야 하고,
그래서 줄마다 어느 해의 기록인지와 근거 문서를 함께 준다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from . import reconstruction as rc, workstate as ws

_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")
_MONTH_DAY = re.compile(r"^\s*(\d{1,2})월\s*(\d{1,2})일\s*$")
_VAGUE = re.compile(r"^\s*(\d{1,2})월\s*(초순|중순|하순|초|중|말)?\s*$")
_VAGUE_RANK = {"초": 1, "초순": 1, "중": 11, "중순": 11, "말": 21, "하순": 21}
# 같은 날 같은 업무의 두 줄이 이만큼 같은 글자를 이어서 품고 있으면 같은 일정이다.
# ('우수공무원 추천(전부서)' · '우수공무원 부서 추천' · '…우수공무원 추천(전부서 및 전동)')
SAME_LINE_CHARS = 5


@dataclass(frozen=True, slots=True)
class Entry:
    month: int
    day: int | None          # 날짜를 모르면 None ('7월 초', '6월')
    when: str                # 화면에 보일 때 — '17일', '초', '중'
    what: str
    task_id: int
    task_name: str
    year: int | None         # 어느 해의 기록인가. 문서에 연도가 없고 문서의 해도 모르면 None
    doc_id: int | None
    filename: str
    path: str
    item_id: int = 0         # 이 날짜가 든 업무 항목. 고치거나 뺄 때 쓴다
    content: str = ""        # 그 항목의 값 전체 ('6월 17일 / 10월 18일')
    confirmed: bool = False  # 사람이 확인한 항목인가

    @property
    def order(self) -> tuple:
        return (self.month, self.day if self.day is not None else _VAGUE_RANK.get(self.when, 32),
                self.what)


def entries(db, *, keep_task_links: bool = False) -> list[Entry]:
    """일정·기한, 달 → 날 순서. 업무별 화면에서는 업무 연결을 보존한다."""
    found: list[Entry] = []
    for task in db.tasks():
        for item in db.task_items(task["id"]):
            if item["kind"] != rc.DEADLINE:
                continue
            what = item["slot"].removeprefix(rc.SCHEDULE_PREFIX)
            attach_years(db, item["evidence"])
            for value in (item["content"] or "").split(" / "):
                entry = _entry(value, what, task, item["evidence"])
                if entry is not None:
                    found.append(replace(entry, item_id=item["id"], content=item["content"] or "",
                                         confirmed=item["human_state"] in ws.CONFIRMED_BY))
    return _dedupe(sorted(found, key=lambda e: e.order), keep_task_links=keep_task_links)


def for_month(found: list[Entry], task_id: int, month: int) -> list[Entry]:
    """이 업무가 그 달에 문서에 적어 둔 일정. '작년 이맘때'를 날짜로 말하는 데 쓴다."""
    return [e for e in found if e.task_id == task_id and e.month == month]


def by_month(found: list[Entry]) -> dict[int, list[Entry]]:
    months: dict[int, list[Entry]] = {}
    for entry in found:
        months.setdefault(entry.month, []).append(entry)
    return months


def _entry(value: str, what: str, task, evidence: list[dict]) -> Entry | None:
    year = day = None
    iso, short, vague = _ISO.match(value), _MONTH_DAY.match(value), _VAGUE.match(value)
    if iso:
        year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        when = f"{day}일"
    elif short:
        month, day = int(short.group(1)), int(short.group(2))
        when = f"{day}일"
    elif vague:
        month, when = int(vague.group(1)), vague.group(2) or ""
    else:
        return None
    if not 1 <= month <= 12:
        return None
    source = _source(evidence, month, day)
    if year is None and source and source.get("doc_id") is not None:
        year = source.get("_year")
    return Entry(month, day, when, what, task["id"], task["name"], year,
                 source.get("doc_id") if source else None,
                 (source.get("filename") or "") if source else "",
                 (source.get("path") or "") if source else "")


def _source(evidence: list[dict], month: int, day: int | None) -> dict | None:
    """이 날짜가 적힌 근거. 한 항목에 날짜가 여럿이면 그 날짜를 품은 인용문을 찾는다."""
    if not evidence:
        return None
    if day is not None:
        pattern = re.compile(rf"(?<!\d)0?{month}\s*[.월]\s*0?{day}(?!\d)")
        for item in evidence:
            if pattern.search(item.get("quote") or ""):
                return item
    else:
        for item in evidence:
            if re.search(rf"(?<!\d)0?{month}\s*월", item.get("quote") or ""):
                return item
    return evidence[0]


def _dedupe(found: list[Entry], *, keep_task_links: bool = False) -> list[Entry]:
    """같은 계획서가 두 업무 폴더에 들어 있거나, 한 계획서가 같은 일정을 요약과 본문에
    다른 말로 적은 경우를 한 줄로 줄인다. 이름이 짧은 쪽을 남긴다."""
    kept: list[Entry] = []
    for entry in found:
        twin = next((index for index, other in enumerate(kept)
                     if (not keep_task_links or other.task_id == entry.task_id)
                     and (other.month, other.day, other.when, other.year)
                     == (entry.month, entry.day, entry.when, entry.year)
                     and _same_line(other.what, entry.what)), None)
        if twin is None:
            kept.append(entry)
        elif len(entry.what) < len(kept[twin].what):
            kept[twin] = entry
    return kept


def _same_line(left: str, right: str) -> bool:
    return rc.same_line(left, right, SAME_LINE_CHARS)


def attach_years(db, found_evidence: list[dict]) -> None:
    """근거 문서의 해를 붙인다(문서에 연도 없이 '6. 17.'만 적힌 일정의 해)."""
    ids = sorted({e["doc_id"] for e in found_evidence if e.get("doc_id") is not None})
    if not ids:
        return
    marks = ", ".join("?" * len(ids))
    years = {row["id"]: row["eff_year"] for row in db.con.execute(
        f"SELECT id, eff_year FROM documents WHERE id IN ({marks})", ids)}
    for item in found_evidence:
        item["_year"] = years.get(item.get("doc_id"))
