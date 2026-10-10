"""도구가 받는 손잡이.

도구는 등급만큼만 할 수 있다. 약속이 아니라 받은 것의 한계다.

    읽기 도구   읽기 전용 연결만 받는다. 쓰려고 하면 SQLite가 거절한다
    초안 도구   읽기 전용 연결과 산출물 작성기를 받는다
    변경 도구   쓰는 연결과 산출물 작성기를 받는다 (승인을 지난 뒤에만 불린다)

원본 파일을 여는 길은 어느 등급에도 없다 — 도구는 이미 분석해 DB에 담긴 내용을
읽는다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from ..db import Database
from . import policy
from .artifacts import ArtifactWriter


class ReadOnlyDatabase(Database):
    """같은 프로젝트를 읽기만 하는 연결. Database의 읽기 함수는 그대로 쓸 수 있다."""

    @property
    def con(self) -> sqlite3.Connection:
        if self._con is None:
            con = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True,
                                  isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            con.execute("PRAGMA foreign_keys = ON")
            self._con = con
        return self._con


@dataclass
class Context:
    db: Database                          # 등급에 따라 읽기 전용이거나 쓰는 연결
    run_id: int
    step_id: int
    task_id: int | None
    case_id: int | None
    today: date                           # 기준일. 도구는 date.today()를 부르지 않는다
    artifacts: ArtifactWriter | None      # 읽기 도구에는 없다
    should_stop: Callable[[], bool] = lambda: False
    client: object | None = None          # 모델이 필요한 선택 단계에서만 쓴다


def build(store: Database, reader: Database, tool_risk: str, *, run_id: int, step_id: int,
          task_id: int | None, case_id: int | None, today: date,
          should_stop: Callable[[], bool], client=None) -> Context:
    """등급에 맞는 손잡이를 만든다. store는 실행기의 연결, reader는 읽기 전용 연결이다."""
    writer = (ArtifactWriter(store, run_id, step_id, case_id)
              if policy.writes_files(tool_risk) else None)
    db = store if tool_risk == policy.CHANGE else reader
    return Context(db=db, run_id=run_id, step_id=step_id, task_id=task_id, case_id=case_id,
                   today=today, artifacts=writer, should_stop=should_stop, client=client)
