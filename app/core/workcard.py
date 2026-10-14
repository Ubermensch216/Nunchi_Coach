"""첫 업무 카드와 결과 초안 (구축계획서 C05, §4.4).

후임자가 업무 하나를 열었을 때 한 화면에서 보는 것들을 모은다. 저장된 항목·
단계·공백을 화면과 초안이 **같은 모양으로** 쓰게 하려고 여기 한 곳에 둔다 —
화면과 내보낸 글이 서로 다른 말을 하면 어느 쪽도 믿을 수 없다.

    해야 할 일          목적과 이번에 낼 것
    현재까지의 기록      마지막 기록과 그 이후 상태
    시작 준비            준비 자료·서식·담당
    처리 순서            문서에 적힌 절차 / 과거 수행 사례 (섞지 않는다)
    다음 행동            지금 할 수 있는 일 → 확인할 일
    근거와 공백          엇갈리거나 빠진 정보와 그것을 찾을 방법
    수행 결과            진행 기록과 결과 문서

초안은 자료에서 확인한 값만 옮겨 적는다. 없는 값은 빈칸으로 둔다 — 채워
넣으면 그 순간부터 근거 없는 값이 문서에 실려 다음 사람에게 넘어간다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import continuity, matters, provenance, reconstruction as rc, workstate as ws

# 카드의 '해야 할 일'과 '시작 준비'에 들어가는 항목 종류.
_TODO_KINDS = (rc.PURPOSE, rc.DEADLINE, rc.RECIPIENT, rc.AMOUNT, rc.OUTPUT)
_PREPARE_KINDS = (rc.INPUT, rc.FORM, rc.CONTACT, rc.REGULATION, rc.AGENCY, rc.RISK)
OUTPUT_DIR = "outputs"


@dataclass(slots=True)
class Card:
    task_id: int
    name: str
    todo: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    prepare: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)       # 사람이 보탠 설명
    found: list[dict] = field(default_factory=list)       # 모델이 고른 원문 문장
    stated_steps: list[dict] = field(default_factory=list)
    precedent_steps: list[dict] = field(default_factory=list)
    conflicting_procedure: dict | None = None
    actions: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    progress: dict = field(default_factory=dict)
    matters: list[dict] = field(default_factory=list)     # 이 업무의 문서에 나온 우리 기관의 건
    matter_others: int = 0                                 # 같은 목록의 다른 기관 행 수(참고)

    @property
    def empty(self) -> bool:
        """자료에서 아무것도 복원하지 못했다(분석 전이거나 문서가 없다)."""
        return not (self.todo or self.records or self.prepare or self.notes or self.found
                    or self.stated_steps or self.precedent_steps)

    def headline(self) -> str:
        """'3단계 중 1단계 완료 · 확인할 것 2건'."""
        total, done = self.progress.get("stated_total", 0), self.progress.get("stated_done", 0)
        parts = []
        if total:
            parts.append(f"{total}단계 중 {done}단계 완료")
        elif self.precedent_steps:
            parts.append(f"과거 수행 사례 {len(self.precedent_steps)}단계")
        if self.gaps:
            parts.append(f"확인할 것 {len(self.gaps)}건")
        return " · ".join(parts) or "복원된 내용이 없습니다"


def build(db, task_id: int) -> Card:
    task = db.task(task_id)
    card = Card(task_id=task_id, name=task["name"] if task else "")
    for item in db.task_items(task_id):
        if item["kind"] in _TODO_KINDS:
            card.todo.append(item)
        elif item["kind"] in _PREPARE_KINDS:
            card.prepare.append(item)
        elif item["kind"] == rc.RECORD:
            card.records.append(item)
        elif item["kind"] == rc.NOTE:
            card.notes.append(item)
        elif item["kind"] == rc.STATEMENT:
            card.found.append(item)
        elif item["kind"] == rc.PROCEDURE and item["basis_state"] == ws.CONFLICT:
            card.conflicting_procedure = item
    order = {kind: index for index, kind in enumerate(_TODO_KINDS + _PREPARE_KINDS)}
    card.todo.sort(key=lambda item: (order[item["kind"]], item["slot"]))
    card.prepare.sort(key=lambda item: (order[item["kind"]], item["slot"]))
    # 마지막 기록이 먼저, 그 이후 상태가 다음.
    card.records.sort(key=lambda item: item["slot"] != rc.SLOT_LAST_RECORD)

    for step in db.work_steps(task_id):
        if step["procedure_kind"] == ws.PROCEDURE_PRECEDENT:
            card.precedent_steps.append(step)
        else:
            card.stated_steps.append(step)
    card.matters = matters.for_task(db, task_id)
    card.matter_others = matters.other_rows(db, task_id)
    card.actions = continuity.next_actions(db, task_id)
    card.gaps = [dict(gap) for gap in db.info_gaps(task_id)]
    card.progress = {
        "stated_total": len(card.stated_steps),
        "stated_done": sum(s["run_state"] == ws.DONE for s in card.stated_steps),
        "in_progress": sum(s["run_state"] == ws.IN_PROGRESS for s in card.stated_steps),
        "blocked": sum(s["blocked"] for s in card.stated_steps),
    }
    return card


_RECORD_LABEL = {rc.SLOT_LAST_RECORD: "마지막 기록", rc.SLOT_AFTERWARDS: "그 이후"}
FOUND_NOTE = ("이름표 없이 문장으로만 적혀 있어 AI가 골라낸 대목입니다. 문장은 문서에 적힌 "
              "그대로이고, 이 업무에 중요한 내용이라는 판단만 추정입니다.")


def item_label(item: dict) -> str:
    """'기한 · 회신기한', '제출처'. 종류와 이름표가 같은 말이면 한 번만 적는다."""
    slot = item["slot"] or ""
    if slot.startswith("user-") and slot[5:].isdigit():
        return rc.KIND_LABEL.get(item["kind"], item["kind"])
    if item["kind"] == rc.RECORD:
        return _RECORD_LABEL.get(item["slot"], rc.KIND_LABEL[rc.RECORD])
    if item["kind"] == rc.STATEMENT:
        return (item["slot"] or "").split(" · ")[0] or rc.KIND_LABEL[rc.STATEMENT]
    name = rc.KIND_LABEL.get(item["kind"], item["kind"])
    slot = item["slot"] or ""
    if not slot or slot.replace(" ", "") == name.replace(" ", ""):
        return name
    return f"{name} · {slot}"


def state_line(row: dict) -> str:
    """'문서에 명시 · 현재 대상에 적용 · 사람 확인 없음'."""
    return ws.describe(row["basis_state"], row["applicability"], row["human_state"])


def evidence_line(db, evidence: dict) -> str:
    """'요청공문.hwpx · 표 1 · 2행 · 온나라(합성) · …'. 원문이 바뀐 근거는 그렇게 밝힌다."""
    parts = [evidence.get("filename") or "원본 없음"]
    if evidence.get("locator"):
        parts.append(evidence["locator"])
    if evidence.get("doc_id"):
        official = provenance.describe(
            provenance.official_sources(db, [evidence["doc_id"]]).get(evidence["doc_id"]))
        if official:
            parts.append(official)
    if not evidence.get("current"):
        parts.append("원문이 바뀌었거나 없음")
    return " · ".join(parts)


# ── 결과 초안 ───────────────────────────────────────────────────────

def default_output_dir(db) -> Path:
    """프로젝트 폴더 안의 outputs. 자료 폴더와 섞이지 않는다."""
    return Path(db.path).parent / OUTPUT_DIR


def draft_filename(card: Card, today: date | None = None) -> str:
    today = today or date.today()
    safe = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in card.name).strip() or "업무"
    return f"{safe}_수행초안_{today:%Y%m%d}.md"


def draft_markdown(db, task_id: int, today: date | None = None) -> str:
    """자료에서 확인한 값·절차·공백을 옮겨 적은 작업용 초안(Markdown).

    공식 인계서나 결재 문서가 아니다. 후임자가 고쳐 쓰거나 기관 서식에 옮겨
    적는 출발점이다.
    """
    today = today or date.today()
    card = build(db, task_id)
    notes: list[str] = []     # 각주처럼 모으는 근거

    def cite(row: dict) -> str:
        marks = []
        for evidence in row.get("evidence") or []:
            line = evidence_line(db, evidence)
            if evidence.get("quote"):
                line += f" — “{evidence['quote']}”"
            if line not in notes:
                notes.append(line)
            marks.append(f"[{notes.index(line) + 1}]")
        return "".join(marks)

    def cell(text: str | None) -> str:
        return " ".join((text or "").split()).replace("|", "\\|")

    out = [f"# {card.name} — 수행 초안", "",
           f"작성 기준일: {today.isoformat()} · 자료에서 자동으로 옮겨 적은 초안입니다. "
           f"빈칸은 자료에서 찾지 못한 값입니다. 확인하지 않은 값을 채워 넣지 않았습니다.", ""]

    out += ["## 해야 할 일", "", "| 항목 | 값 | 상태 | 근거 |", "|---|---|---|---|"]
    present = {item["kind"] for item in card.todo}
    for item in card.todo:
        shown = item["content"] if item["basis_state"] != ws.CONFLICT else f"(엇갈림) {item['content']}"
        out.append(f"| {cell(item_label(item))} | {cell(shown)} "
                   f"| {state_line(item)} | {cite(item)} |")
    for kind in (rc.DEADLINE, rc.RECIPIENT):
        if kind not in present:
            # 빈칸으로 남긴다. 추정한 값을 넣지 않는다.
            out.append(f"| {rc.KIND_LABEL[kind]} |  | 자료 없음 |  |")
    out.append("")

    out += ["## 현재까지의 기록", ""]
    out += [f"- {item['content']} ({ws.basis_label(item['basis_state'])}) {cite(item)}".rstrip()
            for item in card.records] or ["- 기록을 찾지 못했습니다."]
    out.append("")

    out += ["## 시작 준비", ""]
    out += [f"- {rc.KIND_LABEL[item['kind']]}: {item['content']} {cite(item)}".rstrip()
            for item in card.prepare] or ["- 자료에 적힌 준비 자료·서식이 없습니다."]
    out.append("")

    if card.found:
        out += ["## 문장에서 찾은 내용", "", FOUND_NOTE, ""]
        out += [f"- {item_label(item)}: {item['content']} {cite(item)}".rstrip()
                for item in card.found]
        out.append("")

    if card.notes:
        out += ["## 보충 설명", "",
                "문서에는 없고 사람이 보탠 내용입니다.", ""]
        out += [f"- {item['content']} ({ws.human_label(item['human_state'])})"
                for item in card.notes]
        out.append("")

    out += ["## 처리 순서", ""]
    if card.stated_steps:
        out += ["문서에 적힌 절차입니다.", "",
                "| 순서 | 단계 | 준비 자료 | 서식 | 완료 확인 | 수행 상태 | 근거 |",
                "|---|---|---|---|---|---|---|"]
        for number, step in enumerate(card.stated_steps, start=1):
            run = ws.run_label(step["run_state"])
            if step["blocked"]:
                run += " (확인할 것 있음)"
            label = step["label"]
            if step["procedure_kind"] == ws.PROCEDURE_TOLD:
                label += f" ({ws.procedure_label(ws.PROCEDURE_TOLD)})"
            out.append(f"| {number} | {cell(label)} | {cell(step['inputs'])} "
                       f"| {cell(step['forms'])} | {cell(step['done_criteria'])} | {run} "
                       f"| {cite(step)} |")
    elif card.conflicting_procedure:
        out.append(f"자료가 서로 다른 절차를 적고 있어 순서를 세우지 않았습니다: "
                   f"{card.conflicting_procedure['content']} {cite(card.conflicting_procedure)}")
    else:
        out.append("문서에 적힌 절차를 찾지 못했습니다.")
    out.append("")
    if card.precedent_steps:
        out += ["과거 수행 사례입니다. 그렇게 했다는 기록이며, 반드시 이 순서로 해야 한다는 "
                "뜻이 아닙니다.", ""]
        out += [f"- {step['label']} — {step['output'] or ''} {cite(step)}".rstrip()
                for step in card.precedent_steps]
        out.append("")

    if card.matters:
        out += ["## 건별 현황", "",
                "목록 문서의 행에서 세운 우리 기관의 건입니다. 진행은 자료에서 확인한 데까지만 적었습니다.", "",
                "| 회차 | 건 | 주관 · 소관 | 자료로 확인한 진행 | 근거 |", "|---|---|---|---|---|"]
        for matter in card.matters:
            owners = " · ".join(p for p in (matter["dept"], matter["ministry"]) if p)
            out.append(f"| {cell(matter['round'])} | {cell(matter['title'])} | {cell(owners)} "
                       f"| {cell(matters.progress_line(matter))} | {cite(matter)} |")
        if card.matter_others:
            out.append("")
            out.append(f"같은 목록의 다른 기관 행 {card.matter_others:,}건은 참고 사례로만 셌습니다.")
        out.append("")

    out += ["## 다음 행동", ""]
    out += [f"- {'[할 수 있는 일]' if action['type'] == 'do' else '[확인]'} {action['text']}"
            for action in card.actions] or ["- 남은 행동이 없습니다."]
    out.append("")

    out += ["## 확인이 필요한 정보", ""]
    if card.gaps:
        for gap in card.gaps:
            out.append(f"- **{gap['field']}** — {gap['detail']}")
            if gap["research"]:
                out.append(f"  - 다시 찾아본 결과: {gap['research']}")
            if gap["next_action"]:
                out.append(f"  - 다음 행동: {gap['next_action']}")
    else:
        out.append("- 없습니다.")
    out.append("")

    out += ["## 수행 기록", ""]
    done = [step for step in card.stated_steps + card.precedent_steps
            if step["run_state"] != ws.NOT_STARTED]
    for step in done:
        line = f"- {step['label']}: {ws.run_label(step['run_state'])}"
        if step["finished_at"]:
            line += f" ({step['finished_at']})"
        if step["result_path"]:
            line += f" · 결과 {step['result_path']}"
        if step["run_note"]:
            line += f" · {step['run_note']}"
        out.append(line)
    if not done:
        out.append("- 아직 기록한 단계가 없습니다.")
    out.append("")

    out += ["## 근거", ""]
    out += [f"{index}. {line}" for index, line in enumerate(notes, start=1)] or ["근거가 없습니다."]
    out.append("")
    return "\n".join(out)
