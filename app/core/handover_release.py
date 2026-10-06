"""업무 복원본 스냅샷 (구축계획서 C06, §5.1·§5.2).

화면의 '지금 할 일'은 살아 있는 자료다 — 분석이 다시 돌거나 사람이 고치면
바뀐다. 스냅샷은 **어느 한 시점에 무엇을 알고 있었는지**를 굳혀 둔 것이다.
다음 담당자에게 넘길 때도, 나중에 "그때 왜 그렇게 했나"를 되짚을 때도
그 시점의 판이 필요하다.

지키는 것
  · 전임자 확인 없이 저장된다. 자료에서 자동으로 복원한 것만으로도 저장하고
    열어 볼 수 있다.
  · 사람 확인을 꾸미지 않는다. 확인하지 않은 값은 스냅샷 안에서도, 내보낸
    글에서도 '사람 확인 없음'이다. 저장했다고 확인한 것이 되지 않는다.
  · 한 번 저장한 스냅샷은 고치지 않는다. 달라지면 새 스냅샷을 만든다.
  · 내보낸 글은 **스냅샷에서** 만든다. 지금 화면에서 다시 만들면 저장한 판과
    다른 글이 나온다.
  · 절대 경로를 담지 않는다. 문서는 자료원 안의 상대 경로와 해시로 적는다 —
    다른 PC에서 자료 폴더를 다시 가리킬 때 그것으로 찾는다(C08).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from . import provenance, reconstruction as rc, workcard, workstate as ws
from .paths import relative_to_source

FORMAT_VERSION = 1


# ── 스냅샷 본체 ─────────────────────────────────────────────────────

def build_payload(db, basis_at: str | None = None) -> dict:
    """지금의 복원 결과를 통째로 담는다. 저장하지 않는다."""
    context = db.work_context()
    documents: dict[int, dict] = {}
    sources = {row["id"]: row for row in db.sources()}

    def cite(evidence_rows) -> list[dict]:
        out = []
        for evidence in evidence_rows:
            doc_id = evidence.get("doc_id")
            if doc_id and doc_id not in documents:
                documents[doc_id] = _document_entry(db, doc_id, sources)
            out.append({
                "doc": doc_id, "doc_name": evidence.get("doc_name") or evidence.get("filename"),
                "doc_relpath": evidence.get("doc_relpath"),
                "locator": evidence.get("locator") or "",
                "quote": evidence.get("quote") or "", "doc_hash": evidence.get("doc_hash"),
                # 저장하는 순간에 이 근거가 원문과 맞았는가.
                "current": bool(evidence.get("current")),
            })
        return out

    tasks = []
    for row in db.tasks():
        card = workcard.build(db, row["id"])
        items = db.task_items(row["id"])
        tasks.append({
            "id": row["id"], "name": row["name"], "description": row["description"] or "",
            "task_review": row["review_state"],
            "items": [{
                "id": item["id"], "kind": item["kind"], "slot": item["slot"],
                "label": workcard.item_label(item), "content": item["content"],
                "auto_content": item["auto_content"], "origin": item["origin"],
                "basis_state": item["basis_state"], "applicability": item["applicability"],
                "human_state": item["human_state"], "version": item["version"],
                "evidence": cite(item["evidence"]),
                "reviews": _reviews(db, "item", item["id"]),
            } for item in items],
            "steps": [{
                "id": step["id"], "slot": step["slot"], "label": step["label"],
                "procedure_kind": step["procedure_kind"], "ordinal": step["ordinal"],
                "inputs": step["inputs"], "forms": step["forms"], "output": step["output"],
                "done_criteria": step["done_criteria"], "origin": step["origin"],
                "basis_state": step["basis_state"], "applicability": step["applicability"],
                "human_state": step["human_state"], "blocked": bool(step["blocked"]),
                "run_state": step["run_state"], "started_at": step["started_at"],
                "finished_at": step["finished_at"], "result_path": step["result_path"],
                "run_note": step["run_note"], "evidence": cite(step["evidence"]),
                "reviews": _reviews(db, "step", step["id"]),
            } for step in card.stated_steps + card.precedent_steps],
            "gaps": [{
                "id": gap["id"], "kind": gap["kind"], "field": gap["field"],
                "detail": gap["detail"], "research": gap["research"],
                "next_action": gap["next_action"], "state": gap["state"],
                "resolution": gap["resolution"], "origin": gap["origin"],
                "gap_key": gap["gap_key"], "step_id": gap["step_id"],
            } for gap in db.info_gaps(row["id"], state=None)],
            "actions": [{"type": action["type"], "text": action["text"]}
                        for action in card.actions],
        })

    return {
        "format": FORMAT_VERSION,
        "basis_at": basis_at or datetime.now().isoformat(timespec="seconds"),
        "context": {"mode": context["mode"] if context else ws.MODE_MATERIALS,
                    "predecessor": context["predecessor"] if context else None},
        "tasks": tasks,
        # 근거로 쓰인 문서만 싣는다. 자료 전체의 목록이 아니다.
        "documents": [documents[key] for key in sorted(documents)],
    }


def _reviews(db, kind: str, target_id: int) -> list[dict]:
    """누가 어떤 자격으로 무엇을 했는가. 다음 사람에게 이력째로 넘어간다."""
    return [{"action": row["action"], "role": row["reviewer_role"],
             "name": row["reviewer_name"], "at": row["created_at"],
             "before": row["before_val"], "after": row["after_val"]}
            for row in db.reviews(kind, target_id)]


def _document_entry(db, doc_id: int, sources: dict) -> dict:
    row = db.document(doc_id)
    source = sources.get(row["source_id"])
    official = provenance.official_sources(db, [doc_id]).get(doc_id)
    return {
        "id": doc_id, "filename": row["filename"], "hash": row["hash"], "size": row["size"],
        "source_kind": source["kind"] if source else "",
        "relative_path": relative_to_source(row["path"], source["path"] if source else ""),
        "eff_date": row["eff_date"],
        "official": provenance.describe(official),
        "official_document_id": official["document_id"] if official else None,
    }


def summarize(payload: dict) -> dict:
    """건수와 미확인 요약. 목록 화면과 글의 머리에 쓴다."""
    items = [item for task in payload["tasks"] for item in task["items"]
             if item["kind"] != rc.RECORD]
    steps = [step for task in payload["tasks"] for step in task["steps"]
             if step["procedure_kind"] != ws.PROCEDURE_PRECEDENT]
    gaps = [gap for task in payload["tasks"] for gap in task["gaps"]]
    return {
        "tasks": len(payload["tasks"]),
        "items": len(items),
        "items_confirmed": sum(item["human_state"] in ws.CONFIRMED_BY for item in items),
        "items_by_predecessor": sum(item["human_state"] == ws.PREDECESSOR for item in items),
        "items_by_successor": sum(item["human_state"] == ws.SUCCESSOR for item in items),
        "items_recheck": sum(item["human_state"] == ws.RECHECK for item in items),
        "conflicts": sum(item["basis_state"] == ws.CONFLICT for item in items),
        "steps": len(steps),
        "steps_done": sum(step["run_state"] == ws.DONE for step in steps),
        "gaps_open": sum(gap["state"] == "open" for gap in gaps),
        "gaps_resolved": sum(gap["state"] == "resolved" for gap in gaps),
        "documents": len(payload["documents"]),
    }


def content_hash(payload: dict) -> str:
    """내용의 지문. 저장 시각만 다른 같은 내용은 같은 지문이다."""
    body = {key: value for key, value in payload.items() if key != "basis_at"}
    text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── 저장 ────────────────────────────────────────────────────────────

def save(db, label: str = "", kind: str = ws.SNAPSHOT_RESTORATION,
         basis_at: str | None = None, review: dict | None = None) -> tuple[int, bool]:
    """지금의 복원 결과를 스냅샷으로 저장한다. (id, 새로 만들었는가)를 돌려준다.

    마지막 스냅샷과 내용이 같으면 새로 만들지 않는다 — 같은 판이 여러 줄
    쌓이면 무엇이 언제 바뀌었는지 읽을 수 없다.
    """
    payload = build_payload(db, basis_at)
    if review:
        payload["review"] = review
    digest = content_hash(payload)
    latest = next(iter(db.snapshots(kind)), None)
    if latest is not None and latest["content_hash"] == digest:
        return latest["id"], False
    context = db.work_context()
    snapshot_id = db.add_snapshot(
        kind, payload["basis_at"], digest,
        json.dumps(summarize(payload), ensure_ascii=False),
        json.dumps(payload, ensure_ascii=False), label,
        context["id"] if context else None)
    return snapshot_id, True


def publish(db, note: str = "", basis_at: str | None = None) -> tuple[int, bool]:
    """전임자가 검토한 인계본을 발행한다 (선택 경로, C07).

    발행은 **내용 전체의 승인이 아니다.** 전임자가 실제로 확인한 항목만
    '전임자 확인'이고, 나머지는 발행 뒤에도 확인하지 않은 값으로 남는다.
    무엇을 확인하지 않았는지는 인계본에 그대로 실린다.

    전임자가 검토자로 지정되어 있지 않으면 발행하지 않는다. 전임자가 보지
    않은 것을 '전임자 검토 인계본'이라고 부를 수 없다 — 그때는 업무
    복원본으로 저장하면 된다.
    """
    name, role = db.reviewer()
    if role != ws.PREDECESSOR:
        raise ValueError("인계본은 전임자가 검토해 발행합니다. 전임자가 참여하지 않았다면 "
                         "‘업무 복원본’으로 저장하세요 — 그것만으로도 이어받을 수 있습니다.")
    review = {"reviewer": name, "role": role, "note": " ".join(note.split())}
    return save(db, label="인계본 발행", kind=ws.SNAPSHOT_HANDOVER, basis_at=basis_at,
                review=review)


def autosave(db) -> tuple[int, bool] | None:
    """분석이 끝날 때 부른다. 복원된 것이 하나도 없으면 빈 판을 만들지 않는다."""
    if not any(not workcard.build(db, row["id"]).empty for row in db.tasks()):
        return None
    return save(db, label="자동 저장")


def load(db, snapshot_id: int) -> dict | None:
    row = db.snapshot(snapshot_id)
    return json.loads(row["payload"]) if row else None


def describe(row) -> str:
    """목록 한 줄: '자료 기반 업무 복원본 · 2025-09-10 · 업무 3개 · 항목 12개 중 2개 사람 확인'."""
    summary = json.loads(row["summary"])
    parts = [ws.SNAPSHOT_LABEL.get(row["kind"], row["kind"]), row["basis_at"][:16].replace("T", " "),
             f"업무 {summary['tasks']}개",
             f"항목 {summary['items']}개 중 {summary['items_confirmed']}개 사람 확인"]
    if row["kind"] == ws.SNAPSHOT_HANDOVER:
        parts.append(f"전임자 확인 {summary.get('items_by_predecessor', 0)}개")
    if summary["gaps_open"]:
        parts.append(f"확인할 것 {summary['gaps_open']}건")
    if row["label"]:
        parts.append(row["label"])
    return " · ".join(parts)


# ── 글로 내보내기 ───────────────────────────────────────────────────

def to_markdown(payload: dict, kind: str = ws.SNAPSHOT_RESTORATION) -> str:
    """스냅샷을 사람이 읽을 글로 옮긴다. 살아 있는 DB가 아니라 스냅샷만 본다."""
    summary = summarize(payload)
    documents = {doc["id"]: doc for doc in payload["documents"]}
    notes: list[str] = []

    def cite(evidence_rows) -> str:
        marks = []
        for evidence in evidence_rows:
            doc = documents.get(evidence["doc"])
            parts = [doc["filename"] if doc else evidence.get("doc_name") or "원본 없음"]
            if not doc:
                parts.append("저장 당시 원문을 찾지 못함")
            if evidence["locator"]:
                parts.append(evidence["locator"])
            if doc and doc["official"]:
                parts.append(doc["official"])
            if not evidence["current"]:
                parts.append("저장 당시 원문과 맞지 않음")
            line = " · ".join(parts)
            if evidence["quote"]:
                line += f" — “{evidence['quote']}”"
            if line not in notes:
                notes.append(line)
            marks.append(f"[{notes.index(line) + 1}]")
        return "".join(marks)

    def cell(text) -> str:
        return " ".join(str(text or "").split()).replace("|", "\\|")

    out = [f"# {ws.SNAPSHOT_LABEL.get(kind, kind)}", "",
           f"- 기준 시각: {payload['basis_at'].replace('T', ' ')}",
           f"- 시작 방식: {'자료만으로 시작' if payload['context']['mode'] == ws.MODE_MATERIALS else '전임자와 함께'}"]
    if payload["context"]["predecessor"]:
        out.append(f"- 전임자: {payload['context']['predecessor']}")
    review = payload.get("review")
    if review:
        who = review["reviewer"] or "(이름 없음)"
        out += [
            f"- 검토·발행: 전임자 {who} — 이 앱에 적어 넣은 표시명이며 본인 확인을 거친 "
            f"서명이 아닙니다.",
            f"- 전임자가 확인한 항목 {summary['items_by_predecessor']}개 · 후임자가 확인한 "
            f"항목 {summary['items_by_successor']}개 · 확인하지 않은 항목 "
            f"{summary['items'] - summary['items_confirmed']}개",
            "- 발행은 내용 전체의 승인이 아닙니다. 전임자가 확인하지 않은 항목은 아래에 "
            "따로 적었습니다.",
        ]
        if review["note"]:
            out.append(f"- 전임자의 말: {review['note']}")
    out += [
        f"- 업무 {summary['tasks']}개 · 항목 {summary['items']}개 · 문서에 적힌 절차 "
        f"{summary['steps']}단계(완료 기록 {summary['steps_done']}) · 확인할 것 {summary['gaps_open']}건",
        # 사람 확인을 부풀리지 않는다. 숫자를 그대로 적는다.
        f"- 사람이 확인한 항목: {summary['items_confirmed']}개 / {summary['items']}개. "
        f"나머지는 자료에서 자동으로 읽은 값이며 사람이 확인하지 않았습니다.",
        "", "이 글은 저장한 시점의 내용입니다. 그 뒤에 자료나 판단이 바뀌었을 수 있습니다. "
        "공식 인계서나 결재 문서가 아닙니다.", ""]

    for task in payload["tasks"]:
        out += [f"## {task['name']}", ""]
        records = [item for item in task["items"] if item["kind"] == rc.RECORD]
        fields = [item for item in task["items"]
                  if item["kind"] not in (rc.RECORD, rc.PROCEDURE)]
        stated = [s for s in task["steps"] if s["procedure_kind"] != ws.PROCEDURE_PRECEDENT]
        cases = [s for s in task["steps"] if s["procedure_kind"] == ws.PROCEDURE_PRECEDENT]

        if fields:
            out += ["| 항목 | 값 | 근거 상태 | 적용 시점 | 사람 확인 | 근거 |",
                    "|---|---|---|---|---|---|"]
            for item in fields:
                value = item["content"]
                if item["basis_state"] == ws.CONFLICT and item["human_state"] not in ws.CONFIRMED_BY:
                    value = f"(엇갈림) {value}"
                out.append(
                    f"| {cell(item['label'])} | {cell(value)} "
                    f"| {ws.basis_label(item['basis_state'])} "
                    f"| {ws.applicability_label(item['applicability'])} "
                    f"| {ws.human_label(item['human_state'])} | {cite(item['evidence'])} |")
                if item["human_state"] == ws.RECHECK and item["auto_content"]:
                    out.append(f"|  | 자료의 값: {cell(item['auto_content'])} |  |  |  |  |")
            out.append("")
        else:
            out += ["문서에 적힌 항목을 찾지 못했습니다.", ""]

        if records:
            out += ["**현재까지의 기록**", ""]
            out += [f"- {item['content']} {cite(item['evidence'])}".rstrip() for item in records]
            out.append("")

        if stated:
            out += ["**처리 순서**", "",
                    "| 순서 | 단계 | 준비 자료 | 서식 | 완료 확인 | 수행 상태 | 사람 확인 | 근거 |",
                    "|---|---|---|---|---|---|---|---|"]
            for number, step in enumerate(stated, start=1):
                run = ws.run_label(step["run_state"])
                if step["result_path"] or step["run_note"]:
                    run += " — " + " · ".join(
                        part for part in (step["result_path"], step["run_note"]) if part)
                if step["blocked"]:
                    run += " (확인할 것 있음)"
                if step["procedure_kind"] == ws.PROCEDURE_TOLD:
                    step = {**step, "label": f"{step['label']} "
                                             f"({ws.procedure_label(ws.PROCEDURE_TOLD)})"}
                out.append(
                    f"| {number} | {cell(step['label'])} | {cell(step['inputs'])} "
                    f"| {cell(step['forms'])} | {cell(step['done_criteria'])} | {cell(run)} "
                    f"| {ws.human_label(step['human_state'])} | {cite(step['evidence'])} |")
            out.append("")
        if cases:
            out += ["**과거 수행 사례** — 그렇게 했다는 기록이며, 반드시 이 순서라는 뜻이 아닙니다.", ""]
            out += [f"- {step['label']} — {step['output'] or ''} {cite(step['evidence'])}".rstrip()
                    for step in cases]
            out.append("")

        if task["actions"]:
            out += ["**다음 행동**", ""]
            out += [f"- {'[할 수 있는 일]' if a['type'] == 'do' else '[확인]'} {a['text']}"
                    for a in task["actions"]]
            out.append("")

        open_gaps = [gap for gap in task["gaps"] if gap["state"] == "open"]
        closed = [gap for gap in task["gaps"] if gap["state"] == "resolved" and gap["resolution"]]
        if open_gaps:
            out += ["**확인이 필요한 정보**", ""]
            for gap in open_gaps:
                out.append(f"- {gap['field']} — {gap['detail']}")
                if gap["research"]:
                    out.append(f"  - 다시 찾아본 결과: {gap['research']}")
                if gap["next_action"]:
                    out.append(f"  - 다음 행동: {gap['next_action']}")
            out.append("")
        if closed:
            out += ["**확인을 마친 정보**", ""]
            out += [f"- {gap['field']} — {gap['resolution']}" for gap in closed]
            out.append("")

    if review:
        out += ["## 전임자가 확인하지 않은 항목", ""]
        unreviewed = [(task["name"], item) for task in payload["tasks"] for item in task["items"]
                      if item["kind"] != rc.RECORD and item["human_state"] != ws.PREDECESSOR]
        out += [f"- {name} · {item['label']}: {item['content']} "
                f"({ws.basis_label(item['basis_state'])} · {ws.human_label(item['human_state'])})"
                for name, item in unreviewed] or ["- 없습니다. 모든 항목을 전임자가 확인했습니다."]
        out.append("")

    out += ["## 근거", ""]
    out += [f"{index}. {line}" for index, line in enumerate(notes, start=1)] or ["근거가 없습니다."]
    out += ["", "## 근거 문서 목록", "",
            "| 문서 | 자료원 안의 위치 | 출처 | 해시(앞 12자) |", "|---|---|---|---|"]
    for doc in payload["documents"]:
        out.append(f"| {cell(doc['filename'])} | {cell(doc['relative_path'])} "
                   f"| {cell(doc['official'] or '개인 폴더 자료')} | {(doc['hash'] or '')[:12]} |")
    out.append("")
    return "\n".join(out)
