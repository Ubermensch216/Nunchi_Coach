"""눈치코치가 파악한 업무를 업무노트의 자료로 넣는다.

업무 하나에서 넣을 수 있는 것은 두 가지다.

    업무 정리본   눈치코치가 자료에서 읽어 정리한 내용 — 설명·주기·항목·처리 순서·
                  확인할 것. 글 한 편으로 엮어 올린 파일과 같은 보관소에 둔다.
    업무의 문서   그 업무에 묶인 전임자 원문. 이미 분석한 문서를 연결만 한다.

정리본은 원문이 아니다. 그래서 글 머리와 항목마다 근거 문서·사람 확인 여부를
적고, 출처 목록에서도 '업무 정리본'으로 따로 보인다. 원문과 함께 넣어 두면
답의 근거 번호로 둘을 가려 볼 수 있다.

정리본은 넣은 때의 판이다. 같은 내용이면 같은 글이 나오도록 날짜·시각을 적지
않는다 — 그래야 업무가 그대로일 때 해시가 같고, 달라졌을 때만 '업무 내용 변경'
으로 알릴 수 있다(outdated). 보관소 문서는 업무 파악의 재료가 아니므로
(Database.work_sources) 정리본이 다시 업무로 묶여 돌아오는 일은 없다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from ..db import representative_predicate
from . import notebook, notebook_ingest, reconstruction as rc, status, workcard, workstate as ws

ORIGIN = "task"
BRIEF_PREFIX = "업무 정리본"
QUOTE_CHARS = 120
READING_LIMIT = 10
_CYCLE_CONFIDENCE = {"high": "높음", "medium": "보통", "low": "낮음"}
_LINKABLE = ("d.hash IS NOT NULL AND d.missing_since IS NULL "
             "AND d.parse_status IN ('ok','partial')")


def _task(db, task_id: int):
    row = db.task(task_id)
    if row is None or row["not_a_task"] or row["merged_into"]:
        raise ValueError("사용 중인 업무를 선택하세요.")
    return row


def brief_filename(name: str) -> str:
    safe = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name).strip() or "업무"
    return f"{BRIEF_PREFIX} - {safe}.md"


def choices(db) -> list[dict]:
    """고르는 화면의 재료 — 업무마다 연결할 수 있는 문서."""
    out = []
    for task in db.tasks():
        documents = db.con.execute(
            "SELECT d.id, d.filename, d.eff_year FROM task_docs td "
            "JOIN documents d ON d.id = td.doc_id "
            f"WHERE td.task_id = ? AND {_LINKABLE} AND {representative_predicate('d')} "
            "ORDER BY d.eff_date DESC NULLS LAST, d.filename", (task["id"],)).fetchall()
        out.append({"id": task["id"], "name": task["name"],
                    "description": task["description"] or "",
                    "state": status.label(status.of_task(task)), "documents": documents})
    return out


# ── 업무 정리본 ─────────────────────────────────────────────────────

def _months(raw: str | None) -> str:
    """'9,10,11' → '9~11월'. 떨어진 달은 '3월·12월'."""
    months = sorted(int(m) for m in (raw or "").split(",") if m.strip())
    if not months:
        return ""
    spans = [[months[0], months[0]]]
    for month in months[1:]:
        if month == spans[-1][1] + 1:
            spans[-1][1] = month
        else:
            spans.append([month, month])
    return "·".join(f"{a}월" if a == b else f"{a}~{b}월" for a, b in spans)


def _cycle_line(row) -> str:
    if row is None:
        return "자료에서 반복 주기를 찾지 못했습니다."
    by = "사람이 확정" if row["decided_by"] == "user" else "자료에서 추정"
    if row["kind"] == "none":
        return f"반복하지 않는 업무입니다 ({by})."
    text = "매월" if row["kind"] == "monthly" else f"매년 {_months(row['months'])}"
    if row["day_hint"]:
        text += f" {row['day_hint']}"
    if row["decided_by"] == "user":
        return f"{text} ({by})"
    confidence = _CYCLE_CONFIDENCE.get(row["confidence"], row["confidence"])
    return f"{text} ({by} · {row['years_observed']}년 확인 · 신뢰도 {confidence})"


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def _cite(row: dict) -> str:
    """' — 근거: 요청.hwpx · 표 1 “제출기한 9월 30일”'. 근거가 없으면 빈 문자열."""
    marks = []
    for evidence in row.get("evidence") or []:
        parts = [evidence.get("filename") or "원본 없음"]
        if evidence.get("locator"):
            parts.append(evidence["locator"])
        if not evidence.get("current"):
            parts.append("원문이 바뀌었거나 없음")
        mark = " · ".join(parts)
        quote = _flat(evidence.get("quote"))
        if quote:
            mark += f" “{quote[:QUOTE_CHARS]}”"
        if mark not in marks:
            marks.append(mark)
    return " — 근거: " + " / ".join(marks) if marks else ""


def _state_text(task) -> str:
    """'자료에서 추정'. 화면의 뱃지 기호(◐)는 글에 싣지 않는다."""
    state = status.of_task(task)
    return status.label(state).removeprefix(status.symbol(state)).strip()


def _item_line(item: dict) -> str:
    value = _flat(item["content"])
    if item["basis_state"] == ws.CONFLICT and item["human_state"] not in ws.CONFIRMED_BY:
        value = f"(자료끼리 엇갈림) {value}"
    return f"{workcard.item_label(item)}: {value} ({workcard.state_line(item)}){_cite(item)}"


def _step_line(kind: str, number: int, step: dict) -> str:
    label = _flat(step["label"])
    if step["procedure_kind"] == ws.PROCEDURE_TOLD:
        label += f" ({ws.procedure_label(ws.PROCEDURE_TOLD)})"
    extras = [f"{name}: {_flat(step[key])}" for name, key in (
        ("준비 자료", "inputs"), ("서식", "forms"), ("산출물", "output"),
        ("완료 확인", "done_criteria")) if _flat(step[key])]
    run = ws.run_label(step["run_state"])
    if step["blocked"]:
        run += " · 확인할 것 있음"
    line = f"{kind} {number}단계: {label}"
    if extras:
        line += " — " + " / ".join(extras)
    return f"{line} ({workcard.state_line(step)} · 수행 {run}){_cite(step)}"


def _past_year(db, task_id: int) -> tuple[int | None, list]:
    """처리 순서가 남아 있는 가장 최근 연도."""
    for row in db.con.execute(
            "SELECT DISTINCT year FROM task_steps WHERE task_id = ? ORDER BY year DESC", (task_id,)):
        steps = db.task_steps(task_id, row["year"])
        if steps:
            return row["year"], steps
    return None, []


def brief_markdown(db, task_id: int) -> str:
    """업무 하나를 눈치코치가 정리한 대로 적은 글.

    한 줄이 한 문단이고, 줄마다 업무 이름을 다시 적는다. 검색은 대목 몇 줄만
    떼어 보므로 이름이 머리에만 있으면 그 기한이 어느 업무의 것인지 알 수 없다.
    줄을 문단으로 끊어 두면 답이 인용한 바로 그 줄이 원문 보기에서 칠해진다.
    """
    task = _task(db, task_id)
    name = task["name"]
    head = f"「{name}」"
    items = db.task_items(task_id)
    steps = db.work_steps(task_id)
    documents = db.task_documents(task_id)
    present = [d for d in documents if not d["missing_since"]]
    years = sorted({d["eff_year"] for d in present if d["eff_year"]})

    def group(*kinds):
        chosen = [item for item in items if item["kind"] in kinds]
        return sorted(chosen, key=lambda item: (kinds.index(item["kind"]), item["slot"] or ""))

    lines = [
        f"# {BRIEF_PREFIX} — {name}",
        f"이 글은 눈치코치가 전임자 자료를 분석해 정리한 업무 {head}의 내용입니다. "
        "원문 문서가 아니라 자료에서 읽어 정리한 것이며, 항목마다 근거 문서와 사람 확인 여부를 "
        "적었습니다. 사람이 확인하지 않은 값은 틀릴 수 있으니 근거 문서를 함께 확인하세요.",
    ]

    def section(title: str, entries: list[str], note: str = "") -> None:
        if entries:
            lines.append(f"## {head} {title}" + (f" — {note}" if note else ""))
            lines.extend(f"- {name} · {entry}" for entry in entries)

    span = f" · {years[0]}~{years[-1]}년" if len(years) > 1 else (f" · {years[0]}년" if years else "")
    overview = [f"업무 이름: {name} ({_state_text(task)})"]
    if _flat(task["description"]):
        overview.append(f"업무 설명: {_flat(task['description'])}")
    overview += [f"관련 문서: {len(present)}건{span}",
                 f"반복 주기: {_cycle_line(db.task_cycle(task_id))}"]
    if not present and "업무분장표에 기재되어" in (task["description"] or ""):
        overview.append("업무분장표에는 있지만 이 업무의 자료를 찾지 못했습니다. 새로 맡은 업무이거나 다른 "
                        "곳에 자료가 있을 수 있습니다 — 넣을 자료가 있으면 업무노트에 추가하세요.")
    section("개요", overview)

    from . import charter
    charted = []
    for found in charter.items_for_task(db, name, task["description"]):
        where = found["document"] + (f" {found['locator'][4:]}행" if found["locator"].startswith("row:") else "")
        detail = f" — 세부업무: {', '.join(found['details'])}" if found["details"] else ""
        charted.append(f"{found['name']}{detail} (근거: {where})")
    section("업무분장", charted, "분장표에 적힌 이 업무")

    section("업무 범위·역할", [_item_line(i) for i in group(rc.SCOPE)],
            "업무 대상·감사 대상 기간·실시 기간·담당 업무")
    section("해야 할 일", [_item_line(i) for i in group(rc.PURPOSE, rc.DEADLINE, rc.RECIPIENT, rc.AMOUNT, rc.OUTPUT)],
            "목적·기한·제출처·금액·산출물")
    section("시작 준비", [_item_line(i) for i in group(rc.INPUT, rc.FORM, rc.CONTACT, rc.REGULATION, rc.AGENCY, rc.RISK)],
            "준비 자료·서식·담당·관련 규정·관계기관·유의사항")
    records = sorted(group(rc.RECORD), key=lambda item: item["slot"] != rc.SLOT_LAST_RECORD)
    section("현재까지의 기록", [_item_line(i) for i in records])
    section("문장에서 찾은 내용", [_item_line(i) for i in group(rc.STATEMENT)], workcard.FOUND_NOTE)
    section("보충 설명", [_item_line(i) for i in group(rc.NOTE)], "문서에는 없고 사람이 보탠 내용")

    stated = [s for s in steps if s["procedure_kind"] != ws.PROCEDURE_PRECEDENT]
    cases = [s for s in steps if s["procedure_kind"] == ws.PROCEDURE_PRECEDENT]
    conflict = next((i for i in items if i["kind"] == rc.PROCEDURE
                     and i["basis_state"] == ws.CONFLICT), None)
    if stated:
        section("처리 순서", [_step_line("처리 순서", n, s) for n, s in enumerate(stated, start=1)],
                "문서에 적힌 절차")
    elif conflict:
        section("처리 순서", ["처리 순서: 자료가 서로 다른 절차를 적고 있어 순서를 세우지 않았습니다 — "
                              f"{_flat(conflict['content'])}{_cite(conflict)}"])
    section("과거 수행 사례", [_step_line("과거 수행 사례", n, s) for n, s in enumerate(cases, start=1)],
            "그렇게 했다는 기록이며, 반드시 이 순서로 해야 한다는 뜻이 아닙니다")

    year, timeline = _past_year(db, task_id)
    entries = []
    for number, step in enumerate(timeline, start=1):
        when = step["day_hint"] or (f"{step['month']}월" if step["month"] else "")
        entry = f"{year}년 {number}번째: " + (f"{when} — " if when else "") + _flat(step["label"])
        if step["filename"]:
            entry += f" (문서: {step['filename']})"
        if step["is_inferred"]:
            entry += " (근거 없는 추정)"
        if step["gap_note"]:
            entry += f" · {_flat(step['gap_note'])}"
        entries.append(entry)
    section(f"{year}년에 문서가 오간 순서", entries,
            "문서 날짜로 되짚은 순서이며 문서에 적힌 절차가 아닙니다")

    entries = []
    for gap in db.info_gaps(task_id):
        entry = f"확인 필요 · {gap['field']}: {_flat(gap['detail'])}"
        if _flat(gap["research"]):
            entry += f" · 다시 찾아본 결과: {_flat(gap['research'])}"
        if _flat(gap["next_action"]):
            entry += f" · 다음 행동: {_flat(gap['next_action'])}"
        entries.append(entry)
    section("확인이 필요한 정보", entries)

    section("먼저 읽을 문서", [
        f"먼저 읽을 문서 {n}: {row['filename']} — {_flat(row['reason'])}"
        for n, row in enumerate(db.task_reading(task_id)[:READING_LIMIT], start=1)])

    return "\n\n".join(lines) + "\n"


def brief_digest(db, task_id: int) -> str:
    return hashlib.sha256(brief_markdown(db, task_id).encode("utf-8")).hexdigest()


# ── 업무노트에 넣기 ───────────────────────────────────────────────────

def add_brief(db, notebook_id: int, task_id: int, client=None,
              on_phase: Callable[[str], None] = lambda phase: None,
              should_stop: Callable[[], bool] = lambda: False) -> notebook_ingest.Ingested:
    """업무 정리본을 지금 내용으로 만들어 업무노트 자료로 넣는다.

    이 업무노트에 같은 업무의 옛 정리본이 있으면 새 판으로 바꾼다. 옛 글은
    보관소에 남는다 — 그 글을 근거로 받은 지난 답을 다시 확인할 수 있어야 한다.
    """
    from . import manual_versions
    task = _task(db, task_id)
    filename = brief_filename(task["name"])
    data = brief_markdown(db, task_id).encode("utf-8")
    with TemporaryDirectory() as folder:
        path = Path(folder) / filename
        path.write_bytes(data)
        outcome = notebook_ingest.ingest_file(db, notebook_id, path, client, on_phase, should_stop,
                                              origin=ORIGIN)
    if outcome.doc_id is None or not outcome.usable:
        # 실패는 판본을 올리지 않고 사유만 남긴다. 사용자가 멈춘 것은 실패가 아니다.
        if not should_stop():
            manual_versions.record_failure(db, task_id, notebook_id,
                                           outcome.error or outcome.note or outcome.status or "알 수 없는 사유")
        return outcome
    manual_versions.record(db, task_id, notebook_id, outcome.doc_id, hashlib.sha256(data).hexdigest())
    with db.transaction():
        old = db.con.execute(
            "SELECT doc_id, selected FROM notebook_sources "
            "WHERE notebook_id=? AND task_id=? AND doc_id!=?",
            (notebook_id, task_id, outcome.doc_id)).fetchall()
        db.con.execute("UPDATE notebook_sources SET origin=?, task_id=? WHERE notebook_id=? AND doc_id=?",
                       (ORIGIN, task_id, notebook_id, outcome.doc_id))
        if old:
            # 쓰지 않던 정리본을 새 판으로 바꿨다고 근거에 다시 넣지 않는다.
            db.con.execute("UPDATE notebook_sources SET selected=? WHERE notebook_id=? AND doc_id=?",
                           (int(any(row["selected"] for row in old)), notebook_id, outcome.doc_id))
            db.con.executemany("DELETE FROM notebook_sources WHERE notebook_id=? AND doc_id=?",
                               [(notebook_id, row["doc_id"]) for row in old])
        db.audit("notebook.task.add", f"{notebook_id}/{task_id}", result=outcome.status)
    if old:
        notebook.refresh_states(db)
    return outcome


def add_documents(db, notebook_id: int, doc_ids) -> int:
    """업무에 묶인 원문을 연결한다. 이미 연결한 것은 그대로 둔다."""
    linked = {row["doc_id"] for row in notebook.sources(db, notebook_id)}
    fresh = set(doc_ids) - linked
    if fresh:
        notebook.add_sources(db, notebook_id, fresh)
    return len(fresh)


def linked_tasks(db, notebook_id: int) -> set[int]:
    """정리본을 이미 넣은 업무."""
    return {row[0] for row in db.con.execute(
        "SELECT task_id FROM notebook_sources WHERE notebook_id=? AND task_id IS NOT NULL",
        (notebook_id,))}


def outdated(db, notebook_id: int) -> dict[int, int]:
    """넣은 뒤 업무 내용이 달라진 정리본 — {문서: 업무}.

    업무가 없어졌거나 합쳐졌으면 견줄 것이 없다. 그 정리본은 넣은 때의 판으로 남는다.
    """
    stale = {}
    for row in db.con.execute(
            "SELECT ns.doc_id, ns.task_id, d.hash FROM notebook_sources ns "
            "JOIN documents d ON d.id = ns.doc_id JOIN tasks t ON t.id = ns.task_id "
            "WHERE ns.notebook_id=? AND t.not_a_task=0 AND t.merged_into IS NULL",
            (notebook_id,)).fetchall():
        if brief_digest(db, row["task_id"]) != row["hash"]:
            stale[row["doc_id"]] = row["task_id"]
    return stale


# ── 업무의 업무노트 열기 ──────────────────────────────────────────────

@dataclass
class Opened:
    notebook_id: int
    created: bool = False
    refreshed: bool = False
    warnings: list[str] = field(default_factory=list)


def open_for_task(db, task_id: int, client=None) -> Opened:
    """업무의 업무노트를 찾거나 만들고, 매뉴얼 정리본과 업무 문서를 연결한다.

    만든 직후이거나 넣어 둔 정리본의 업무 내용이 달라졌으면 새 판으로 바꾼다.
    일부가 실패해도 업무노트는 연다 — 무엇이 빠졌는지는 warnings로 알려, 조용히
    빈 업무노트를 보여 주지 않는다.
    """
    task = _task(db, task_id)
    books = [nb for nb in notebook.overview(db) if not nb["archived"]]
    target = next((nb for nb in books if nb["task_id"] == task_id), None)         or next((nb for nb in books if nb["title"] == task["name"]), None)
    created = target is None
    if created:
        notebook_id = notebook.create(db, title=task["name"], task_id=task_id)
    else:
        notebook_id = target["id"]
        if not target["task_id"]:
            with db.transaction():
                db.con.execute("UPDATE notebooks SET task_id=? WHERE id=?", (task_id, notebook_id))
    opened = Opened(notebook_id, created=created)
    stale = created or task_id not in linked_tasks(db, notebook_id)         or task_id in set(outdated(db, notebook_id).values())
    if stale:
        try:
            outcome = add_brief(db, notebook_id, task_id, client)
            if outcome.usable:
                opened.refreshed = True
            else:
                opened.warnings.append("매뉴얼 정리본을 넣지 못했습니다: "
                                       + (outcome.error or outcome.note or outcome.status))
        except Exception as exc:   # 정리본 실패가 업무노트 열기를 막지 않는다
            from . import diagnostics, manual_versions
            diagnostics.record("notebook.task.brief.error", exc)
            manual_versions.record_failure(db, task_id, notebook_id, f"{type(exc).__name__}: {exc}")
            opened.warnings.append(f"매뉴얼 정리본을 넣지 못했습니다: {type(exc).__name__}: {exc}")
    if created:
        try:
            doc_ids = [row["id"] for row in db.task_documents(task_id)
                       if row["hash"] and not row["missing_since"] and row["parse_status"] in ("ok", "partial")]
            if doc_ids:
                add_documents(db, notebook_id, doc_ids)
        except Exception as exc:
            opened.warnings.append(f"업무 문서를 연결하지 못했습니다: {type(exc).__name__}: {exc}")
    return opened


def auto_generate_for_tasks(db) -> list[int]:
    """분석 완료 후 적격 업무들에 대해 자동으로 업무노트(AI 매뉴얼)를 사전 생성한다.

    대상:
      - not_a_task=0, merged_into IS NULL
      - loose(묶음) 업무 제외
      - 문서 2건 이상
    모델 호출 없이 규칙 기반 정리본만 넣는다.
    """
    created_ids = []
    for task in db.tasks():
        if task["not_a_task"] or task["merged_into"]:
            continue
        if "kind" in task.keys() and task["kind"] == "loose":
            continue
        docs = [row for row in db.task_documents(task["id"])
                if row["hash"] and not row["missing_since"] and row["parse_status"] in ("ok", "partial")]
        if len(docs) < 2:
            continue
        try:
            opened = open_for_task(db, task["id"], client=None)
            if opened.created:
                created_ids.append(opened.notebook_id)
        except Exception as exc:
            from . import diagnostics
            diagnostics.record("notebook.auto_generate.error", exc)
    return created_ids


SEEN_META = "manual_seen:{}"


def version_notice(db, notebook_id: int) -> str:
    """지난번에 이 업무노트를 연 뒤 재분석으로 정리본 판이 올랐으면 그 알림. 없으면 빈 문자열.

    처음 여는 업무노트는 알릴 것이 없다 — 본 판을 적어 두기만 한다.
    """
    from . import manual_versions
    row = db.con.execute("SELECT task_id FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
    last = manual_versions.latest(db, row["task_id"]) if row is not None and row["task_id"] else None
    if last is None:
        return ""
    seen = db.get_meta(SEEN_META.format(notebook_id)) or ""
    if not seen.isdigit():
        mark_version_seen(db, notebook_id)
        return ""
    if last["version"] <= int(seen):
        return ""
    return (f"업무 내용이 바뀌어 정리본을 판 {seen} → {last['version']}로 바꿨습니다. "
            "지난 답의 인용은 그때의 판으로 계속 열립니다.")


def mark_version_seen(db, notebook_id: int) -> None:
    from . import manual_versions
    row = db.con.execute("SELECT task_id FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
    last = manual_versions.latest(db, row["task_id"]) if row is not None and row["task_id"] else None
    if last is not None:
        db.set_meta(SEEN_META.format(notebook_id), str(last["version"]))


def apply_answer(db, notebook_id: int, kind: str, content: str) -> int:
    """업무노트 대화에서 확인한 사실을 그 업무의 항목으로 올린다(S6 왕복).

    사람이 보탠 항목으로 남아 다음 정리본 판에 실린다. 업무에 묶이지 않은 업무노트는 받지 않는다.
    """
    row = db.con.execute("SELECT task_id FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
    if row is None or not row["task_id"]:
        raise ValueError("업무에 묶인 업무노트에서만 업무에 반영할 수 있습니다.")
    name, role = db.reviewer()
    item_id = db.add_task_item(row["task_id"], kind, content, reviewer_name=name, reviewer_role=role)
    db.audit("notebook.apply_answer", f"{notebook_id}/{row['task_id']}")
    return item_id


def archive_for_task(db, task_id: int, archive: bool = True) -> None:
    """업무 합치기 또는 '업무 아님' 시 해당 업무의 노트북을 보관(archive) 처리하거나 복원한다."""
    with db.transaction():
        db.con.execute(
            "UPDATE notebooks SET archived=?, updated_at=datetime('now') WHERE task_id=?",
            (1 if archive else 0, task_id),
        )


def suggested_questions(db, task_id: int) -> list[str]:
    """S6: 업무 종류·다음 일정·공백에서 만든 추천 질문 5개."""
    task = _task(db, task_id)
    questions = []

    # 1. 기한 및 제출처
    items = db.task_items(task_id)
    has_deadline = any(i["kind"] == rc.DEADLINE for i in items)
    if has_deadline:
        questions.append("다음 제출 기한과 제출처는 어디인가요?")
    else:
        questions.append("이 업무의 정기적인 보고나 제출 기한이 언제인가요?")

    # 2. 서식 및 준비물
    has_form = any(i["kind"] == rc.FORM for i in items)
    if has_form:
        questions.append("작년에는 어떤 서식과 양식으로 제출했나요?")
    else:
        questions.append("업무 진행에 필요한 서식이나 증빙 자료는 무엇인가요?")

    # 3. 공문 및 회신
    off_count = db.con.execute(
        "SELECT COUNT(*) FROM task_docs td JOIN source_assets a ON a.doc_id=td.doc_id WHERE td.task_id=?",
        (task_id,)
    ).fetchone()[0]
    if off_count > 0:
        questions.append("접수된 공문 중 후속 조치나 회신이 필요한 건이 있나요?")
    else:
        questions.append("이 업무의 근거가 되는 지침이나 상급기관 공문이 있나요?")

    # 4. 일정/절차
    cycle = db.task_cycle(task_id)
    if cycle and cycle["kind"] != "none":
        questions.append("연간 추진 일정과 단계별 처리 순서는 어떻게 되나요?")
    else:
        questions.append("이 업무를 시작할 때 가장 먼저 해야 할 일은 무엇인가요?")

    # 5. 확인 필요/주의사항
    gaps = db.info_gaps(task_id)
    if gaps:
        questions.append(f"전임자 자료에서 비어 있는 '{gaps[0]['field']}' 정보는 어떻게 확인하나요?")
    else:
        questions.append("전임자가 남긴 메모나 유의사항 중 주의할 점은 무엇인가요?")

    return questions[:5]
