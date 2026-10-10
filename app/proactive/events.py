"""이벤트 기반 감지 — 올해 공식 문서가 들어왔다 / 준비해 둔 초안의 기준이 달라졌다.

‘올해 공식 문서’는 업무에 묶인 시행문·평가지표 가운데 **평가 연도가 그 해로 적힌** 것이다
(actions/tools/common.judge_target과 같은 기준). 올해 작성했다는 것만으로는 올해 기준이
아니다.

두 경우를 가른다.
  · 아직 준비하지 않았다            → 올해 공식 문서가 들어왔으니 준비를 시작하자는 제안
  · 사전조사용 초안을 만들어 뒀다   → 그 초안은 올해 기준이 아니었다. 낡음으로 표시하고
                                       다시 만들어 변경점을 보자는 제안 (설계서 T12)
이미 공식 문서로 만든 산출물이 있고 그 문서의 판이 그대로면 아무것도 하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Event:
    task_id: int
    year: int
    doc_ids: tuple[int, ...]

    def fingerprint(self) -> str:
        """문서 집합의 지문. 문서가 더해지거나 바뀌면 다른 이벤트다."""
        return hashlib.sha256(json.dumps(sorted(self.doc_ids)).encode()).hexdigest()[:10]


def official_documents(db, task_id: int, year: int) -> list[int]:
    return [row["id"] for row in db.con.execute(
        "SELECT DISTINCT d.id FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "JOIN doc_roles r ON r.doc_id = d.id AND r.role IN ('directive', 'criteria') "
        "JOIN doc_years y ON y.doc_id = d.id AND y.kind = 'evaluation' AND y.year = ? "
        "WHERE td.task_id = ? AND d.missing_since IS NULL AND d.parse_status IN ('ok', 'partial') "
        "ORDER BY d.id", (year, task_id))]


def detect(db, today: date) -> list[Event]:
    """오늘 기준 올해와 내년의 공식 문서가 있는 업무."""
    events = []
    for task in db.tasks():
        for year in (today.year, today.year + 1):
            docs = official_documents(db, task["id"], year)
            if docs:
                events.append(Event(task["id"], year, tuple(docs)))
    return events


def snapshot_hashes(db, case_id: int) -> set[str]:
    """아직 낡지 않은 산출물이 만들 때 본 문서의 판들."""
    seen: set[str] = set()
    for row in db.con.execute(
            "SELECT source_snapshot FROM artifacts WHERE case_id = ? AND state NOT IN ('stale', 'superseded')",
            (case_id,)):
        seen |= {item["hash"] for item in json.loads(row["source_snapshot"]) if item.get("hash")}
    return seen


def up_to_date(db, case_id: int, event: Event) -> bool:
    """공식 문서의 지금 판이 모두 낡지 않은 산출물에 쓰였는가."""
    hashes = {row["hash"] for row in db.con.execute(
        f"SELECT hash FROM documents WHERE id IN ({', '.join('?' * len(event.doc_ids))})", event.doc_ids)}
    return bool(hashes) and hashes <= snapshot_hashes(db, case_id)
