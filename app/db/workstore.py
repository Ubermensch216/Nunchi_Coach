"""업무 복원·수행 저장 계층 (스키마 v5, 구축계획서 §5.1·§5.2).

Database에 섞어 쓰는 믹스인이다. repo.py가 이미 1,500줄이라 따로 뗐을 뿐,
연결·트랜잭션·감사 기록은 Database의 것을 그대로 쓴다.

지키는 규칙
  · 전임자·검토자·기존 인계본 없이 만들어진다. 어느 것도 필수 입력이 아니다.
  · 자동 재분석은 사람이 손댄 것을 덮지 않는다. 자료가 달라졌으면 값은 그대로
    두고 '재확인 필요'로 표시하며, 자료가 말하는 새 값을 옆에 보관한다.
  · 근거는 문서의 그때 해시와 함께 남긴다. 원문이 바뀌면 그 근거는 '지금
    본문의 근거'가 아니다.
  · 공백은 자기가 가리키는 단계만 막는다.
  · 수행 완료는 사용자가 명시적으로 기록한 것만이다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from ..core import workstate as ws
from ..core.paths import relative_to_source

# 표마다 다른 것: (표, 근거 소유자, 보이는 값 열, 자동 값 열, 그 밖에 갱신할 열)
_ITEMS = ("task_items", "item", "content", "auto_content", ())
_STEPS = ("work_steps", "step", "label", "auto_label",
          ("ordinal", "inputs", "forms", "output", "done_criteria",
           "procedure_kind", "task_step_id", "actor"))


class WorkStore:
    # ── 시작 맥락 ───────────────────────────────────────────────────
    def start_work_context(
        self, mode: str = ws.MODE_MATERIALS, period_start: str | None = None,
        period_end: str | None = None, predecessor: str | None = None,
        note: str | None = None,
    ) -> int:
        """복원을 시작한 방식과 기준 시각을 남긴다. 전임자 정보는 선택이다."""
        ws.require(mode, ws.MODES, "시작 방식")
        cursor = self.con.execute(
            "INSERT INTO work_contexts(mode, period_start, period_end, predecessor, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (mode, period_start, period_end, (predecessor or "").strip() or None, note),
        )
        self.audit("work.start", str(cursor.lastrowid), detail=mode)
        return cursor.lastrowid

    def work_context(self, context_id: int | None = None):
        """지정한 맥락, 없으면 가장 최근 것."""
        if context_id is None:
            return self.con.execute(
                "SELECT * FROM work_contexts ORDER BY id DESC LIMIT 1").fetchone()
        return self.con.execute(
            "SELECT * FROM work_contexts WHERE id = ?", (context_id,)).fetchone()

    # ── 자동 복원 결과 반영 ─────────────────────────────────────────
    def sync_task_items(self, task_id: int, candidates: Iterable[dict],
                        context_id: int | None = None, extractor: str = "") -> dict[str, int]:
        """자료에서 읽어 낸 항목 후보로 이 업무의 자동 항목을 맞춘다.

        후보: {kind, slot, content, basis_state, applicability, evidence:[(doc_id, locator, quote)]}
        후보에 extractor가 있으면 그 항목에는 그것을 적는다(규칙과 모델이 섞여 들어온다).
        """
        return self._sync(_ITEMS, task_id, candidates, context_id, extractor)

    def sync_work_steps(self, task_id: int, candidates: Iterable[dict],
                        context_id: int | None = None) -> dict[str, int]:
        """자료에서 복원한 수행 단계 후보로 자동 단계를 맞춘다.

        후보: {slot, label, ordinal, inputs, forms, output, done_criteria,
               procedure_kind, basis_state, applicability, task_step_id, evidence}
        """
        return self._sync(_STEPS, task_id, candidates, context_id, "")

    def _sync(self, spec, task_id: int, candidates: Iterable[dict],
              context_id: int | None, extractor: str) -> dict[str, int]:
        table, owner, text_col, auto_col, extra = spec
        result = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0, "recheck": 0}
        is_item = table == "task_items"

        def key_of(row) -> tuple:
            return (row["kind"], row["slot"]) if is_item else (row["slot"],)

        with self.transaction():
            existing = {key_of(row): row for row in self.con.execute(
                f"SELECT * FROM {table} WHERE task_id = ?", (task_id,))}
            seen: set[tuple] = set()

            for candidate in candidates:
                text = " ".join(str(candidate.get(text_col) or "").split())
                slot = str(candidate.get("slot") or "")
                key = (candidate["kind"], slot) if is_item else (slot,)
                if not text or key in seen:
                    continue
                seen.add(key)
                basis = ws.require(candidate.get("basis_state", ws.STATED), ws.BASIS, "근거 상태")
                applies = ws.require(candidate.get("applicability", ws.UNKNOWN),
                                     ws.APPLICABILITY, "적용 시점")
                values = {name: candidate.get(name) for name in extra}
                if "procedure_kind" in values:
                    values["procedure_kind"] = ws.require(
                        values["procedure_kind"] or ws.PROCEDURE_PRECEDENT,
                        ws.PROCEDURE, "절차 유형")
                if "ordinal" in values:
                    values["ordinal"] = int(values["ordinal"] or 0)
                if "actor" in values:
                    values["actor"] = values["actor"] or ""
                old = existing.get(key)

                if old is None:
                    columns = {"task_id": task_id, "context_id": context_id, "slot": slot,
                               text_col: text, auto_col: text, "basis_state": basis,
                               "applicability": applies, **values}
                    if is_item:
                        columns["kind"] = candidate["kind"]
                        columns["extractor"] = candidate.get("extractor") or extractor
                    marks = ", ".join("?" * len(columns))
                    row_id = self.con.execute(
                        f"INSERT INTO {table}({', '.join(columns)}) VALUES ({marks})",
                        list(columns.values()),
                    ).lastrowid
                    self._replace_evidence(owner, row_id, candidate.get("evidence") or ())
                    result["added"] += 1
                    continue

                if self._touched(table, old):
                    # 사람이 손댔거나 이미 수행 중이다. 보이는 값은 그대로 두고,
                    # 자료가 지금 말하는 값만 옆에 적는다.
                    if (old[auto_col] or "") != text:
                        self.con.execute(
                            f"UPDATE {table} SET {auto_col} = ?, basis_state = ?, "
                            f"human_state = CASE WHEN human_state IN ('predecessor','successor') "
                            f"THEN 'recheck' ELSE human_state END, "
                            f"updated_at = datetime('now') WHERE id = ?",
                            (text, basis, old["id"]),
                        )
                        result["recheck"] += 1
                    else:
                        result["unchanged"] += 1
                    continue

                changed = old[text_col] != text
                assignments = {text_col: text, auto_col: text, "basis_state": basis,
                               "applicability": applies, **values}
                if is_item:
                    assignments["extractor"] = candidate.get("extractor") or extractor
                sets = ", ".join(f"{name} = ?" for name in assignments)
                self.con.execute(
                    f"UPDATE {table} SET {sets}, version = version + ?, "
                    f"updated_at = datetime('now') WHERE id = ?",
                    (*assignments.values(), int(changed), old["id"]),
                )
                self._replace_evidence(owner, old["id"], candidate.get("evidence") or ())
                result["updated" if changed else "unchanged"] += 1

            for key, old in existing.items():
                if key in seen or old["origin"] == "user":
                    continue
                if self._touched(table, old):
                    # 확인까지 한 항목인데 자료에서 더는 보이지 않는다. 지우지 않는다.
                    if old["basis_state"] != ws.MISSING:
                        self.con.execute(
                            f"UPDATE {table} SET basis_state = 'missing', {auto_col} = NULL, "
                            f"human_state = CASE WHEN human_state IN ('predecessor','successor') "
                            f"THEN 'recheck' ELSE human_state END, "
                            f"updated_at = datetime('now') WHERE id = ?", (old["id"],))
                        result["recheck"] += 1
                    continue
                self.con.execute(
                    "DELETE FROM evidence WHERE owner_kind = ? AND owner_id = ?",
                    (owner, old["id"]))
                self.con.execute(f"DELETE FROM {table} WHERE id = ?", (old["id"],))
                result["removed"] += 1
        return result

    def _touched(self, table: str, row) -> bool:
        """자동 재분석이 덮으면 안 되는 행인가."""
        if row["origin"] == "user" or row["human_state"] != ws.NONE or row["excluded"]:
            return True
        if table == "work_steps":
            run = self.con.execute(
                "SELECT state FROM step_runs WHERE step_id = ?", (row["id"],)).fetchone()
            return run is not None and run["state"] != ws.NOT_STARTED
        return False

    def _replace_evidence(self, owner: str, owner_id: int,
                          evidence: Sequence[Sequence[Any]]) -> None:
        self.con.execute(
            "DELETE FROM evidence WHERE owner_kind = ? AND owner_id = ?", (owner, owner_id))
        for doc_id, locator, quote in evidence:
            # 문서의 지금 해시를 함께 적는다 — 이 인용이 어느 판에서 나왔는지다.
            # 이름과 자료원 안의 위치도 적어, 문서 번호가 달라지는 다른 PC에서
            # 같은 원문을 다시 찾을 수 있게 한다.
            document = self.con.execute(
                "SELECT d.hash, d.filename, d.path, s.path AS root FROM documents d "
                "LEFT JOIN sources s ON s.id = d.source_id WHERE d.id = ?", (doc_id,)).fetchone()
            self.add_evidence(
                owner, owner_id, doc_id, document["hash"] if document else None, locator, quote,
                document["filename"] if document else None,
                relative_to_source(document["path"], document["root"] or "") if document else None)

    def add_evidence(self, owner: str, owner_id: int, doc_id: int | None, doc_hash: str | None,
                     locator: str, quote: str, doc_name: str | None,
                     doc_relpath: str | None) -> int:
        return self.con.execute(
            "INSERT INTO evidence(owner_kind, owner_id, doc_id, doc_hash, locator, quote, "
            "doc_name, doc_relpath) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner, owner_id, doc_id, doc_hash, locator or "",
             " ".join(str(quote or "").split())[:500], doc_name, doc_relpath)).lastrowid

    # ── 읽기 ────────────────────────────────────────────────────────
    def evidence_for(self, owner_kind: str, owner_ids: Sequence[int]) -> dict[int, list[dict]]:
        """소유자별 근거. `current`가 거짓이면 원문이 바뀌었거나 사라진 근거다."""
        ids = [int(i) for i in owner_ids]
        if not ids:
            return {}
        marks = ", ".join("?" * len(ids))
        out: dict[int, list[dict]] = {}
        for row in self.con.execute(
            f"SELECT e.*, d.filename, d.path, d.hash AS hash_now, d.missing_since "
            f"FROM evidence e LEFT JOIN documents d ON d.id = e.doc_id "
            f"WHERE e.owner_kind = ? AND e.owner_id IN ({marks}) ORDER BY e.id",
            (owner_kind, *ids),
        ):
            item = dict(row)
            # 원문을 못 찾았어도 무엇의 근거였는지는 보여 준다.
            item["filename"] = item["filename"] or item["doc_name"]
            item["current"] = bool(
                item["doc_id"] is not None and item["missing_since"] is None
                and item["doc_hash"] is not None and item["doc_hash"] == item["hash_now"])
            out.setdefault(item["owner_id"], []).append(item)
        return out

    def task_items(self, task_id: int, include_excluded: bool = False) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM task_items WHERE task_id = ? "
            + ("" if include_excluded else "AND excluded = 0 ")
            + "ORDER BY kind, slot, id", (task_id,)).fetchall()
        evidence = self.evidence_for("item", [row["id"] for row in rows])
        return [{**dict(row), "evidence": evidence.get(row["id"], []),
                 "display_state": ws.display_state(row["basis_state"], row["human_state"])}
                for row in rows]

    def work_steps(self, task_id: int, include_excluded: bool = False) -> list[dict]:
        """수행 단계와 각 단계의 수행 상태·열린 공백 수.

        `blocked`는 그 단계를 직접 가리키는 열린 공백이 있을 때만 참이다.
        다른 단계의 공백이나 업무 전체의 공백은 이 단계를 막지 않는다.
        """
        rows = self.con.execute(
            "SELECT s.*, COALESCE(r.state, 'not_started') AS run_state, r.started_at, "
            "       r.finished_at, r.result_doc_id, r.result_path, r.note AS run_note, "
            "       (SELECT COUNT(*) FROM info_gaps g "
            "         WHERE g.step_id = s.id AND g.state = 'open') AS open_gaps "
            "FROM work_steps s LEFT JOIN step_runs r ON r.step_id = s.id "
            "WHERE s.task_id = ? " + ("" if include_excluded else "AND s.excluded = 0 ")
            + "ORDER BY s.ordinal, s.id", (task_id,)).fetchall()
        evidence = self.evidence_for("step", [row["id"] for row in rows])
        return [{**dict(row), "evidence": evidence.get(row["id"], []),
                 "blocked": bool(row["open_gaps"]),
                 "display_state": ws.display_state(row["basis_state"], row["human_state"])}
                for row in rows]

    # ── 사람의 보충·검토 (선택) ─────────────────────────────────────
    def add_task_item(self, task_id: int, kind: str, content: str, slot: str = "",
                      reviewer_name: str = "", reviewer_role: str = ws.SUCCESSOR) -> int:
        """사람이 직접 보탠 항목. 자료에 근거가 없으므로 근거 상태는 '자료 없음'이다."""
        ws.require(reviewer_role, ws.CONFIRMED_BY, "검토자 역할")
        content = " ".join(content.split())
        if not content:
            raise ValueError("내용을 입력해 주세요.")
        with self.transaction():
            slot = slot or f"user-{self.con.execute('SELECT COALESCE(MAX(id), 0) + 1 FROM task_items').fetchone()[0]}"
            item_id = self.con.execute(
                "INSERT INTO task_items(task_id, kind, slot, content, origin, basis_state, "
                "human_state) VALUES (?, ?, ?, ?, 'user', 'missing', ?)",
                (task_id, kind, slot, content, reviewer_role),
            ).lastrowid
            self._review("item", item_id, 1, "supplement", None, content,
                         reviewer_name, reviewer_role)
        return item_id

    def review_task_item(self, item_id: int, action: str, content: str | None = None,
                         reviewer_name: str = "", reviewer_role: str = ws.SUCCESSOR) -> None:
        self._review_row(_ITEMS, item_id, action, content, reviewer_name, reviewer_role)

    def review_work_step(self, step_id: int, action: str, label: str | None = None,
                         reviewer_name: str = "", reviewer_role: str = ws.SUCCESSOR) -> None:
        self._review_row(_STEPS, step_id, action, label, reviewer_name, reviewer_role)

    def _review_row(self, spec, row_id: int, action: str, text: str | None,
                    reviewer_name: str, reviewer_role: str) -> None:
        """확인·수정·제외. 무엇을 했는지와 누가 했는지를 함께 남긴다."""
        table, owner, text_col, _auto_col, _extra = spec
        ws.require(action, ws.REVIEW_ACTIONS, "검토 행동")
        ws.require(reviewer_role, ws.CONFIRMED_BY, "검토자 역할")
        with self.transaction():
            row = self.con.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
            if row is None:
                raise ValueError("대상을 찾을 수 없습니다.")
            before = row[text_col]
            after = before
            version = row["version"]
            if action in ("edit", "supplement"):
                after = " ".join((text or "").split())
                if not after:
                    raise ValueError("내용을 입력해 주세요.")
                version += int(after != before)
                self.con.execute(
                    f"UPDATE {table} SET {text_col} = ?, origin = 'user', human_state = ?, "
                    f"version = ?, updated_at = datetime('now') WHERE id = ?",
                    (after, reviewer_role, version, row_id))
            elif action == "exclude":
                self.con.execute(
                    f"UPDATE {table} SET excluded = 1, human_state = ?, "
                    f"updated_at = datetime('now') WHERE id = ?", (reviewer_role, row_id))
            else:
                self.con.execute(
                    f"UPDATE {table} SET human_state = ?, updated_at = datetime('now') "
                    f"WHERE id = ?", (reviewer_role, row_id))
            self._review(owner, row_id, version, action, before, after,
                         reviewer_name, reviewer_role)

    def _review(self, kind: str, target_id: int, version: int, action: str,
                before: str | None, after: str | None, name: str, role: str) -> None:
        self.con.execute(
            "INSERT INTO reviews(target_kind, target_id, target_version, action, before_val, "
            "after_val, reviewer_name, reviewer_role) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, target_id, version, action, before, after, (name or "").strip(), role))
        # 감사 기록에는 내용도 이름도 남기지 않는다(SEC-005).
        self.audit(f"work.review.{action}", f"{kind}:{target_id}", result=role)

    def reviews(self, target_kind: str, target_id: int) -> list:
        return self.con.execute(
            "SELECT * FROM reviews WHERE target_kind = ? AND target_id = ? ORDER BY id",
            (target_kind, target_id)).fetchall()

    # ── 사람의 작업이 걸린 업무 ─────────────────────────────────────
    # 업무 재발견(ai/discover.py)은 아직 확인하지 않은 업무 제안을 지우고 다시
    # 만든다. 업무 행이 지워지면 그 아래의 항목·단계·수행 기록이 연쇄로 함께
    # 지워지므로, 사람이 무엇이든 해 둔 업무는 그 대상에서 빼야 한다.
    HUMAN_WORK_SQL = (
        "SELECT task_id FROM task_items "
        " WHERE origin = 'user' OR human_state != 'none' OR excluded = 1 "
        "UNION SELECT task_id FROM work_steps "
        " WHERE origin = 'user' OR human_state != 'none' OR excluded = 1 "
        "UNION SELECT s.task_id FROM step_runs r JOIN work_steps s ON s.id = r.step_id "
        " WHERE r.state != 'not_started' "
        "UNION SELECT task_id FROM info_gaps "
        " WHERE origin = 'user' OR (state = 'resolved' AND COALESCE(resolution, '') != '"
        + "새 자료에서 확인되어 자동으로 닫았습니다." + "')"
    )

    def tasks_with_human_work(self) -> set[int]:
        """사람이 고치거나 확인했거나 수행을 기록했거나 공백을 닫은 업무."""
        return {row["task_id"] for row in self.con.execute(self.HUMAN_WORK_SQL)}

    # ── 지금 확인하는 사람 (선택) ───────────────────────────────────
    def reviewer(self) -> tuple[str, str]:
        """(표시명, 역할). 정하지 않았으면 이름 없는 후임자다.

        표시명은 로컬 이력에 남기는 이름일 뿐 본인 확인이 아니다.
        """
        role = self.get_meta("reviewer_role") or ws.SUCCESSOR
        return self.get_meta("reviewer_name") or "", (
            role if role in ws.CONFIRMED_BY else ws.SUCCESSOR)

    def set_reviewer(self, name: str, role: str) -> None:
        """확인·보충을 누구의 것으로 남길지 정한다.

        전임자가 참여하기 시작하면 시작 맥락을 하나 더 남긴다. 이미 있는
        항목·단계·수행 기록은 그대로다 — 참여 방식이 바뀌었다고 다시 만들지 않는다.
        """
        ws.require(role, ws.CONFIRMED_BY, "검토자 역할")
        name = " ".join((name or "").split())
        with self.transaction():
            self.set_meta("reviewer_name", name)
            self.set_meta("reviewer_role", role)
            if role == ws.PREDECESSOR:
                context = self.work_context()
                if (context is None or context["mode"] != ws.MODE_WITH_PREDECESSOR
                        or (context["predecessor"] or "") != name):
                    self.start_work_context(ws.MODE_WITH_PREDECESSOR, predecessor=name)
        self.audit("work.reviewer", result=role)   # 이름은 감사 기록에 남기지 않는다

    def add_work_step(self, task_id: int, label: str, reviewer_name: str = "",
                      reviewer_role: str = ws.SUCCESSOR) -> int:
        """문서에 없던 단계를 사람이 보탠다. 적힌 절차의 맨 뒤에 놓인다."""
        ws.require(reviewer_role, ws.CONFIRMED_BY, "검토자 역할")
        label = " ".join(label.split())
        if not label:
            raise ValueError("내용을 입력해 주세요.")
        with self.transaction():
            # 과거 사례(100번대)보다 앞, 문서에 적힌 단계보다 뒤.
            ordinal = self.con.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM work_steps "
                "WHERE task_id = ? AND procedure_kind != 'precedent'", (task_id,)).fetchone()[0]
            number = self.con.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM work_steps").fetchone()[0]
            step_id = self.con.execute(
                "INSERT INTO work_steps(task_id, slot, ordinal, label, procedure_kind, origin, "
                "basis_state, human_state) VALUES (?, ?, ?, ?, 'told', 'user', 'missing', ?)",
                (task_id, f"user-{number}", ordinal, label, reviewer_role)).lastrowid
            self._review("step", step_id, 1, "supplement", None, label,
                         reviewer_name, reviewer_role)
        return step_id

    # ── 정보 공백 ───────────────────────────────────────────────────
    def add_info_gap(self, task_id: int, kind: str, field: str, detail: str = "",
                     step_id: int | None = None, item_id: int | None = None,
                     research: str | None = None, next_action: str | None = None) -> int:
        ws.require(kind, ws.GAP_KINDS, "공백 종류")
        return self.con.execute(
            "INSERT INTO info_gaps(task_id, step_id, item_id, kind, field, detail, research, "
            "next_action) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, step_id, item_id, kind, field, detail, research, next_action),
        ).lastrowid

    AUTO_RESOLVED = "새 자료에서 확인되어 자동으로 닫았습니다."

    def sync_info_gaps(self, task_id: int, gaps: Iterable[dict]) -> dict[str, int]:
        """분석이 찾은 공백으로 이 업무의 자동 공백을 맞춘다.

        공백: {gap_key, kind, field, detail, research, next_action, step_id, item_id, evidence}

          · 사람이 직접 적은 공백(origin='user')은 건드리지 않는다.
          · 사람이 '해결됨'으로 닫은 공백은 분석이 다시 찾아도 열지 않는다.
            어떻게 해결했는지는 그 사람이 안다.
          · 더는 찾아지지 않는 열린 공백은 자동으로 닫는다 — 새 자료가 메운 것이다.
          · 자동으로 닫혔던 공백이 다시 찾아지면 다시 연다.
        """
        result = {"opened": 0, "updated": 0, "closed": 0, "reopened": 0, "kept_resolved": 0}
        with self.transaction():
            existing = {row["gap_key"]: row for row in self.con.execute(
                "SELECT * FROM info_gaps WHERE task_id = ? AND origin = 'auto' ORDER BY id",
                (task_id,))}
            seen: set[str] = set()
            for gap in gaps:
                key = gap["gap_key"]
                if key in seen:
                    continue
                seen.add(key)
                ws.require(gap["kind"], ws.GAP_KINDS, "공백 종류")
                values = (gap["kind"], gap.get("field", ""), gap.get("detail", ""),
                          gap.get("research"), gap.get("next_action"),
                          gap.get("step_id"), gap.get("item_id"))
                old = existing.get(key)
                if old is None:
                    gap_id = self.con.execute(
                        "INSERT INTO info_gaps(kind, field, detail, research, next_action, "
                        "step_id, item_id, task_id, origin, gap_key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?)",
                        (*values, task_id, key)).lastrowid
                    result["opened"] += 1
                elif old["state"] == "resolved" and old["resolution"] != self.AUTO_RESOLVED:
                    result["kept_resolved"] += 1
                    continue
                else:
                    gap_id = old["id"]
                    self.con.execute(
                        "UPDATE info_gaps SET kind = ?, field = ?, detail = ?, research = ?, "
                        "next_action = ?, step_id = ?, item_id = ?, state = 'open', "
                        "resolution = NULL, resolved_at = NULL WHERE id = ?", (*values, gap_id))
                    result["reopened" if old["state"] == "resolved" else "updated"] += 1
                self._replace_evidence("gap", gap_id, gap.get("evidence") or ())

            for key, old in existing.items():
                if key not in seen and old["state"] == "open":
                    self.con.execute(
                        "UPDATE info_gaps SET state = 'resolved', resolution = ?, "
                        "resolved_at = datetime('now') WHERE id = ?",
                        (self.AUTO_RESOLVED, old["id"]))
                    result["closed"] += 1
        return result

    def resolve_info_gap(self, gap_id: int, resolution: str = "") -> None:
        self.con.execute(
            "UPDATE info_gaps SET state = 'resolved', resolution = ?, "
            "resolved_at = datetime('now') WHERE id = ? AND state = 'open'",
            (resolution, gap_id))
        self.audit("work.gap.resolve", str(gap_id))

    def info_gaps(self, task_id: int, state: str | None = "open") -> list:
        if state is None:
            return self.con.execute(
                "SELECT * FROM info_gaps WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return self.con.execute(
            "SELECT * FROM info_gaps WHERE task_id = ? AND state = ? ORDER BY id",
            (task_id, state)).fetchall()

    # ── 스냅샷 (복원본·인계본) ──────────────────────────────────────
    def add_snapshot(self, kind: str, basis_at: str, content_hash: str, summary: str,
                     payload: str, label: str = "", context_id: int | None = None) -> int:
        """스냅샷을 한 줄 더한다. 고치는 통로는 일부러 두지 않았다."""
        ws.require(kind, ws.SNAPSHOT_KINDS, "스냅샷 종류")
        snapshot_id = self.con.execute(
            "INSERT INTO snapshots(kind, label, context_id, basis_at, content_hash, summary, "
            "payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, label.strip(), context_id, basis_at, content_hash, summary, payload),
        ).lastrowid
        self.audit("work.snapshot", str(snapshot_id), result=kind)
        return snapshot_id

    def snapshots(self, kind: str | None = None) -> list:
        """최신순 목록. 본체(payload)는 싣지 않는다 — 목록 화면에는 요약이면 된다."""
        columns = "id, kind, label, context_id, basis_at, content_hash, summary, created_at"
        if kind:
            return self.con.execute(
                f"SELECT {columns} FROM snapshots WHERE kind = ? ORDER BY id DESC",
                (kind,)).fetchall()
        return self.con.execute(f"SELECT {columns} FROM snapshots ORDER BY id DESC").fetchall()

    def snapshot(self, snapshot_id: int):
        return self.con.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()

    def add_receipt(self, snapshot_id: int, content_hash: str, receiver_name: str,
                    relink: str) -> int:
        """가져온 사실을 남긴다. 받았다는 기록이지 내용을 승인했다는 기록이 아니다."""
        receipt_id = self.con.execute(
            "INSERT INTO snapshot_receipts(snapshot_id, content_hash, receiver_name, relink) "
            "VALUES (?, ?, ?, ?)",
            (snapshot_id, content_hash, (receiver_name or "").strip(), relink)).lastrowid
        self.audit("work.snapshot.receive", str(snapshot_id))
        return receipt_id

    def receipts(self) -> list:
        return self.con.execute(
            "SELECT * FROM snapshot_receipts ORDER BY id DESC").fetchall()

    # ── 수행 기록 ───────────────────────────────────────────────────
    def set_step_run(self, step_id: int, state: str, result_doc_id: int | None = None,
                     result_path: str | None = None, note: str | None = None) -> None:
        """후임자가 이 단계를 어디까지 했는지 적는다.

        처음 진행·완료로 바뀐 시각을 지우지 않는다. 완료를 되돌려도 언제
        시작했는지는 남는다. 결과 문서·경로·메모는 주어진 것만 갱신한다.
        """
        ws.require(state, ws.RUN, "수행 상태")
        with self.transaction():
            if self.con.execute(
                    "SELECT 1 FROM work_steps WHERE id = ?", (step_id,)).fetchone() is None:
                raise ValueError("단계를 찾을 수 없습니다.")
            self.con.execute(
                "INSERT INTO step_runs(step_id) VALUES (?) ON CONFLICT(step_id) DO NOTHING",
                (step_id,))
            self.con.execute(
                "UPDATE step_runs SET state = ?, "
                " started_at = CASE WHEN ? IN ('in_progress','done','waiting') "
                "   THEN COALESCE(started_at, datetime('now')) ELSE started_at END, "
                " finished_at = CASE WHEN ? = 'done' THEN datetime('now') ELSE NULL END, "
                " result_doc_id = COALESCE(?, result_doc_id), "
                " result_path = COALESCE(?, result_path), note = COALESCE(?, note), "
                " updated_at = datetime('now') WHERE step_id = ?",
                (state, state, state, result_doc_id, result_path, note, step_id))
            self.audit("work.step.run", str(step_id), result=state)

    def work_progress(self, task_id: int) -> dict[str, int]:
        """이 업무의 수행 단계가 상태별로 몇 개인가(제외한 단계는 세지 않는다)."""
        counts = {state: 0 for state in ws.RUN}
        for row in self.con.execute(
            "SELECT COALESCE(r.state, 'not_started') AS state, COUNT(*) AS n "
            "FROM work_steps s LEFT JOIN step_runs r ON r.step_id = s.id "
            "WHERE s.task_id = ? AND s.excluded = 0 GROUP BY 1", (task_id,)):
            counts[row["state"]] = row["n"]
        counts["total"] = sum(counts[state] for state in ws.RUN)
        return counts
