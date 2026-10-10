"""C1·C2 — 올해 업무 체크리스트 만들기와 등록 (계획서 2단계).

'평가 준비하기'가 아니라 실행할 수 있는 단위로 쓴다. 네 가지 재료를 합친다.

    올해 공식 문서가 말하는 단계        official   확인된 필수 작업
    문서에 적힌 절차                    stated     올해 적용은 확인 필요
    과거 사례                           precedent  참고
    시스템이 보탠 준비 행동             suggested  추천 작업

과거 관행에서 나온 단계가 올해도 반드시 해야 하는 공식 업무라고 단정하지 않는다. 그래서
출처를 항목마다 적고, 올해 공식 문서가 없으면 기한은 비워 둔다(과거 기한은 설명에만
‘참고’로 적는다).

두 단계로 나뉜다.
    checklist   초안을 파일로 만든다 (초안 등급). 항목은 JSON, 읽기 쉬운 글은 별도 파일.
    register    승인한 초안을 수행 건의 체크 항목으로 올린다 (변경 등급, 승인 필요).
                사람이 고친 항목은 다시 올려도 덮지 않는다.
"""

from __future__ import annotations

import json
from datetime import date

from ...core import workstate as ws, yearcompare as yc
from .. import artifacts as ledger, policy
from ..registry import REGISTRY, Blocked, Tool, ToolError
from . import common

CHECKLIST_TITLE, VIEW_TITLE = "올해 업무 체크리스트", "올해 업무 체크리스트 (보기)"
PHASES = ("기준 확인", "자료 준비", "처리 절차", "자료 수합", "보고 준비")
SOURCE_LABEL = {
    "official": "확인된 필수 작업(올해 공식 문서)", "stated": "문서에 적힌 절차 · 올해 적용은 확인 필요",
    "precedent": "과거 사례 · 참고", "suggested": "추천 작업", "user": "담당자가 알려 준 단계",
}
STEP_SOURCE = {ws.PROCEDURE_PRECEDENT: "precedent", ws.PROCEDURE_SUGGESTED: "suggested",
               ws.PROCEDURE_TOLD: "user"}
FINDINGS_IN_LIST = 3
PRECEDENT_LIMIT = 8
TITLE_CHARS = 60


def _month_day(when: date) -> str:
    return f"{when.month}월 {when.day}일"


def build_items(db, name: str, today: date, target: common.Target, cmp: yc.YearComparison,
                mat: yc.Materials, task_id: int, form_artifact: int | None,
                findings) -> list[dict]:
    year = target.year
    items: list[dict] = []

    def add(slot, phase, title, description="", source="suggested", after=None, **extra) -> dict:
        item = {"slot": slot, "phase": phase, "title": title, "description": description,
                "source_type": source, "after": after, **extra}
        items.append(item)
        return item

    # ── 기준 확인 ──
    if target.official:
        add("base:plan", "기준 확인", f"{year}년 공식 평가계획·시행문 확인",
            f"{target.how}. 등록된 문서가 올해 기준이 맞는지 확인합니다.", "official", state="review")
    else:
        add("base:plan", "기준 확인", f"{year}년 공식 평가계획·시행문 확보",
            f"올해 평가 연도가 적힌 시행문·평가지표가 문서 중에 없습니다. 공식 문서를 확보해 수행 건의 근거로 정하세요.",
            "suggested")
    last = cmp.pairs[-1] if cmp.pairs else None
    summary = ""
    if last is not None and last.indicators_state == yc.OK:
        count = last.counts()
        summary = (f" 직전 비교({last.earlier}→{last.later}년): 달라짐 {count['changed']}, "
                   f"새로 생김 {count['added']}, 보이지 않음 {count['removed']}, 확인 필요 {count['uncertain']}.")
    add("base:diff", "기준 확인", f"{year - 1}년 대비 평가지표·증빙요건 변경사항 확인",
        "비교 보고서의 지표 절을 올해 공식 지표와 맞대어 봅니다." + summary, "suggested", after="base:plan")
    if target.due:
        add("base:due", "기준 확인", f"{year}년 제출기한 확인", "시행문에 적힌 기한입니다. 맞는지 확인하세요."
            if target.due_source == "official" else "담당자가 정한 기한입니다.",
            "official", due_date=target.due.isoformat(), due_state=target.due_source,
            state="review", after="base:plan")
    else:
        past = _past_due(db, cmp, mat)
        add("base:due", "기준 확인", f"{year}년 제출기한 확인",
            "올해 기한은 확인되지 않았습니다." + (f" (참고: {past} — 올해 기한이 아닙니다.)" if past else ""),
            "suggested", after="base:plan")
    for number, (ref, sentence) in enumerate(findings[:FINDINGS_IN_LIST], 1):
        add(f"base:finding:{number}", "기준 확인",
            "지난 평가에서 지적된 사항 점검" if number == 1 else f"지난 평가에서 지적된 사항 점검 ({number})",
            f"과거 지적사항 — {sentence} 올해도 같은 기준인지 확인하세요.", "precedent",
            evidence=[{"doc_id": ref.doc_id, "locator": ref.locator, "quote": ref.quote}])

    # ── 자료 준비 ──
    prep = add("prep:form", "자료 준비", "부서별 실적 취합 서식 검토·확정",
               "초안이 만들어져 있습니다. 부서에 나누기 전에 열과 지표를 검토하세요."
               + ("" if target.official else " 올해 공식 기준 전이므로 사전조사용입니다."),
               "suggested", after="base:diff", approval_required=True)
    if form_artifact:
        prep.update(state="review", completion_kind="system", completion_ref=f"artifact:{form_artifact}")
    add("prep:departments", "자료 준비", "취합 대상 부서·담당자 확인",
        "작년 실적표에 나온 부서가 참고용으로 서식에 들어 있습니다. 올해 대상으로 고치세요.", "suggested", after="prep:form")

    # ── 처리 절차 (문서에 적힌 것과 과거 사례) ──
    # 문서에 적힌 절차가 있으면 그것만 싣는다. 과거 사례(문서마다 붙은 단계 이름)는 같은
    # 이름이 판마다 되풀이되어 목록을 어지럽히므로, 적힌 절차가 없을 때만 이름별로
    # 한 번씩 싣는다.
    steps = db.work_steps(task_id)
    stated = [s for s in steps if s["procedure_kind"] == ws.PROCEDURE_STATED]
    if stated:
        steps = [s for s in steps if s["procedure_kind"] != ws.PROCEDURE_PRECEDENT]
    else:
        seen_labels: set[str] = set()
        kept = []
        for step in steps:
            label = step["label"].split(" (")[0]
            if step["procedure_kind"] == ws.PROCEDURE_PRECEDENT and label in seen_labels:
                continue
            seen_labels.add(label)
            kept.append(step)
        steps = kept[:PRECEDENT_LIMIT]
    step_evidence = db.evidence_for("step", [s["id"] for s in steps])
    for step in steps:
        if step["procedure_kind"] == ws.PROCEDURE_STATED:
            source = "official" if step["applicability"] == ws.CURRENT else "stated"
        else:
            source = STEP_SOURCE.get(step["procedure_kind"], "precedent")
        evidence = [{"doc_id": e["doc_id"], "locator": e["locator"], "quote": e["quote"]}
                    for e in step_evidence.get(step["id"], []) if e["doc_id"]]
        add(f"step:{step['id']}", "처리 절차", step["label"][:TITLE_CHARS] or "(이름 없는 단계)",
            ws.applicability_label(step["applicability"]) if source in ("official", "stated")
            else ws.procedure_label(step["procedure_kind"]),
            source, work_step_id=step["id"], evidence=evidence)

    # ── 자료 수합 · 보고 준비 ──
    add("collect:request", "자료 수합", "부서별 자료 제출 요청문 초안 작성",
        "요청 공문은 초안만 만들 수 있습니다. 발송은 이 프로그램이 하지 않습니다.", "suggested",
        after="prep:departments", approval_required=True)
    add("collect:check", "자료 수합", "회신 실적의 중복·증빙 누락 검사",
        "중복 의심은 지우지 않고 표시만 합니다. 실적 인정 여부는 담당자가 정합니다.", "suggested", after="collect:request")
    add("report:draft", "보고 준비", "실적 보고서 초안 작성·검증", "취합한 실적과 근거를 맞대어 확인합니다.",
        "suggested", after="collect:check")
    submit = add("report:submit", "보고 준비", "결재 및 공식 제출 절차·기한 확인",
                 "결재 상신과 제출은 담당자가 직접 합니다.", "suggested", after="report:draft")
    if target.due:
        submit.update(due_date=target.due.isoformat(), due_state=target.due_source)

    for number, item in enumerate(items, 1):
        item["code"] = f"C-{number:02d}"
    return items


def _past_due(db, cmp: yc.YearComparison, mat: yc.Materials) -> str:
    """지난 해의 기한 한 줄. 올해 기한이 아니라 참고로만 쓴다."""
    for row in yc.read_items(db, cmp.years, mat):
        if row.kind == "deadline" and row.values:
            year = max(row.values)
            return f"{year}년에는 {row.values[year][0]}"
    return ""


def render_view(name: str, today: date, target: common.Target, items: list[dict]) -> str:
    out = [f"# {target.year}년 {name} 준비 체크리스트", "",
           f"만든 날 {today.isoformat()} · 상태 **{'올해 공식 기준 확인됨' if target.official else '초안 · 올해 공식 기준 미확인'}**", "",
           "> 출처 표시를 보세요. ‘확인된 필수 작업’만 올해 공식 문서에 근거한 것입니다. "
           "과거 사례와 문서에 적힌 절차는 올해도 해야 하는 일이라고 단정하지 않습니다.", ""]
    for phase in PHASES:
        group = [item for item in items if item["phase"] == phase]
        if not group:
            continue
        out += [f"## {phase}"]
        for item in group:
            box = "x" if item.get("state") == "done" else " "
            due = f" · 기한 {item['due_date']}" if item.get("due_date") else ""
            approval = " · 승인 필요" if item.get("approval_required") else ""
            out.append(f"- [{box}] **{item['code']}** {item['title']}{due}{approval}")
            out.append(f"  - 출처: {SOURCE_LABEL[item['source_type']]}" + (f" — {item['description']}" if item["description"] else ""))
        out.append("")
    return "\n".join(out)


def run_checklist(context, params: dict) -> dict:
    db = context.db
    found = params["inputs"]["discover"]
    name = common.task_name(db, found["task_id"])
    doc_ids = [item["id"] for item in found["included"]]
    mat = yc.materials(db, doc_ids)
    target = common.judge_target(db, context.case_id, doc_ids, params["target_year"], mat)
    cmp = yc.build(db, doc_ids, target.year, include_target=target.official)
    findings = yc.past_findings(db, mat, before=target.year)
    form_id = params["inputs"].get("form", {}).get("artifacts", {}).get("form")

    items = build_items(db, name, context.today, target, cmp, mat, found["task_id"], form_id, findings)
    evidence = [e for item in items for e in item.get("evidence", [])]
    snapshot = common.sources_of(db, doc_ids)
    mode = "official" if target.official else "preliminary"
    payload = {"target_year": target.year, "mode": mode, "items": items}
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    stem = f"{target.year}_{name}_체크리스트"
    data_id = context.artifacts.save("checklist", CHECKLIST_TITLE, stem + ".json", data, mode=mode,
                                     basis_year=target.year, sources=snapshot, evidence=evidence)
    view_id = context.artifacts.save("checklist_view", VIEW_TITLE, stem + ".md",
                                     render_view(name, context.today, target, items).encode("utf-8"),
                                     mode=mode, basis_year=target.year, sources=snapshot)
    sources = {kind: sum(1 for i in items if i["source_type"] == kind) for kind in SOURCE_LABEL}
    return {"artifact_id": data_id, "view_id": view_id, "items": len(items), "sources": sources,
            "with_due_date": sum(1 for i in items if i.get("due_date")), "mode": mode}


def register_scope(db, params: dict) -> dict:
    """승인이 묶이는 내용 — 올릴 체크리스트 파일의 지문."""
    row = db.artifact(params["inputs"]["checklist"]["artifact_id"])
    return {"artifact": row["id"], "sha256": row["sha256"], "items": params["inputs"]["checklist"]["items"]}


def run_register(context, params: dict) -> dict:
    db = context.db
    if not context.case_id:
        raise Blocked("체크 항목을 올릴 수행 건이 없습니다. 업무에서 ‘준비 작업 실행’으로 시작해 주세요.")
    row = db.artifact(params["inputs"]["checklist"]["artifact_id"])
    if row is None or not ledger.current(db, row):
        raise ToolError("승인한 체크리스트 파일이 바뀌었거나 없어졌습니다. 다시 실행해 주세요.")
    with open(row["path"], "rb") as handle:
        items = json.loads(handle.read().decode("utf-8"))["items"]
    counts = db.sync_checklist(context.case_id, context.run_id, items)
    db.set_artifact_state(row["id"], "reviewed")
    return {"checklist": counts}


REGISTRY.register(Tool("checklist", "1", policy.DRAFT, run_checklist, label="체크리스트 초안"))
REGISTRY.register(Tool("register_checklist", "1", policy.CHANGE, run_register, label="체크 항목으로 올리기",
                       scope=register_scope))
