"""H1·H2 — 자료 제출 요청문 초안과 서식 배포본 확정 (계획서 4단계).

요청문
  부서에 보낼 공문의 **초안**이다. 이 프로그램은 공문을 발송하지 않는다 — 발송 도구 자체가
  없다(actions/policy.py). 사람이 고쳐 직접 시행한다.

  초안에 채우는 값은 확인된 것뿐이다.
    · 제출 기한   올해 공식 문서나 담당자가 정한 날짜가 있을 때만. 없으면 ‘확인 필요’
    · 서식        만들어 둔 취합 서식의 이름
    · 작성 방법   서식의 안내를 옮긴 것
  수신·관련 공문·회신 받을 곳은 문서에서 알 수 없으므로 빈칸(확인 필요)이다. 지난해
  요청문의 문구는 본문이 아니라 ‘참고’ 절에 출처와 함께 따로 싣는다.

서식 확정
  사람이 검토한 서식을 배포본으로 굳힌다(변경 등급, 승인 필요). 승인은 서식 파일의 지문에
  묶이므로 승인한 뒤 서식이 달라지면 쓰지 못한다.
"""

from __future__ import annotations

from ...core import docrole, yearcompare as yc
from ...studio import render, templates
from .. import artifacts as ledger, policy
from ..registry import REGISTRY, Tool, ToolError
from . import common, followup
from .form import NOT_OFFICIAL

DRAFT_TITLE, HWPX_TITLE = "자료 제출 요청문 초안", "자료 제출 요청문 초안 (한글 문서)"
PRELIMINARY_NOTE = ("초안 · 사전조사용 — 올해 공식 평가기준이 확인되지 않았습니다. 지표·기한을 확인해 고친 뒤 "
                    "쓰세요. 이 프로그램은 공문을 발송하지 않습니다. (확인 필요)")
OFFICIAL_NOTE = "초안 — 이 프로그램은 공문을 발송하지 않습니다. 고쳐 쓴 뒤 직접 시행하세요."
REFERENCE_LINES = 4
BLANK = "(확인 필요)"


def prior_wording(db, inputs: followup.Inputs) -> tuple[list[str], list]:
    """지난해 시행문의 앞머리 줄들. (줄, 근거). 올해 문구가 아니라 참고다."""
    for year in sorted((y for y in inputs.mat.by_year if y < inputs.target.year), reverse=True):
        for doc_id in inputs.mat.docs(year, docrole.DIRECTIVE):
            lines, refs = [], []
            for found_section in db.con.execute(
                    "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal", (doc_id,)):
                for line in found_section["text"].splitlines():
                    line = " ".join(line.split())
                    if line and not line.startswith(("수신", "붙임")):
                        lines.append(f"{line}  ({common.filename(db, doc_id)} — {found_section['locator']})")
                        refs.append(yc.Ref(doc_id, found_section["locator"], line))
                    if len(lines) >= REFERENCE_LINES:
                        return lines, refs
            if lines:
                return lines, refs
    return [], []


def render_request(db, inputs: followup.Inputs, form_row, today) -> tuple[str, list]:
    target, name = inputs.target, inputs.name
    year = target.year
    due = (f"{target.due.isoformat()} ({'공식 기한' if target.due_source == 'official' else '담당자가 정한 기한'})"
           if target.due else BLANK)
    out = [f"# {year}년 {name} 부서별 실적 제출 요청(안)", "",
           "> " + (OFFICIAL_NOTE if target.official else PRELIMINARY_NOTE), "",
           f"수신: (취합 대상 부서장 {BLANK})",
           f"제목: {year}년 {name} 부서별 실적 제출 요청", "",
           f"1. 관련: (근거 공문 번호·일자 {BLANK})",
           f"2. {year}년 {name}과(와) 관련하여 아래와 같이 부서별 실적 자료를 요청하오니, 기한까지 제출하여 주시기 바랍니다.",
           "", "## 요청 내용",
           f"- 제출 기한: {due}",
           f"- 회신 받을 곳: (담당 부서·담당자·연락처 {BLANK})",
           f"- 제출 서식: 붙임 서식 ‘{form_row['title']}’ ({year}년 {name} 실적 취합 서식)",
           "- 작성 방법",
           "  - 03_부서별실적 시트에 실적 한 건을 한 줄로 작성합니다.",
           "  - 증빙자료 칸에 증빙 문서의 이름을 적고, 증빙 파일을 함께 제출합니다.",
           "  - 개인정보(주민등록번호·전화번호 등)는 적지 않습니다.",
           "", "붙임: 부서별 실적 취합 서식 1부. 끝.", ""]
    lines, refs = prior_wording(db, inputs)
    if lines:
        out += [f"## 지난해 시행문의 문구 (참고 — {year}년에 적용되는지 확인 필요)", *(f"- {line}" for line in lines), ""]
    return "\n".join(out), refs


def run_request(context, params: dict) -> dict:
    db = context.db
    inputs = followup.case_inputs(context)
    form_row = followup.form_artifact(db, inputs.case["id"])
    text, refs = render_request(db, inputs, form_row, context.today)
    mode = "official" if inputs.target.official else "preliminary"
    stem = f"{inputs.target.year}_{inputs.name}_자료제출_요청문_초안"
    snapshot = common.sources_of(db, inputs.doc_ids)
    md_id = context.artifacts.save("request_draft", DRAFT_TITLE, stem + ".md", text.encode("utf-8"), mode=mode,
                                   basis_year=inputs.target.year, sources=snapshot,
                                   evidence=common.evidence_dicts(refs))
    try:
        data = render.to_hwpx(text, templates.DEFAULT_FORMAT, f"{inputs.target.year}년 {inputs.name} 자료 제출 요청")
    except Exception as exc:                      # 한글 문서를 못 그려도 글 초안은 이미 있다
        raise ToolError(f"한글 문서 초안을 만들지 못했습니다 ({type(exc).__name__}). 글 초안은 만들어 두었습니다.") from exc
    hwpx_id = context.artifacts.save("request_draft_hwpx", HWPX_TITLE, stem + ".hwpx", data, mode=mode,
                                     basis_year=inputs.target.year, sources=snapshot)
    return {"artifact_id": md_id, "hwpx_id": hwpx_id, "form_id": form_row["id"], "mode": mode,
            "due_filled": bool(inputs.target.due), "blanks": text.count(BLANK)}


def finalize_scope(db, params: dict) -> dict:
    """승인이 묶이는 내용 — 확정할 서식 파일의 지문."""
    row = db.artifact(params["inputs"]["request_draft"]["form_id"])
    return {"artifact": row["id"], "sha256": row["sha256"], "title": row["title"], "state": row["state"]}


def run_finalize(context, params: dict) -> dict:
    db = context.db
    form_row = db.artifact(params["inputs"]["request_draft"]["form_id"])
    if form_row is None or form_row["state"] in ("stale", "superseded"):
        raise ToolError("확정하려던 서식이 낡았거나 새 판으로 바뀌었습니다. ‘준비 작업’을 다시 실행해 주세요.")
    if not ledger.current(db, form_row):
        raise ToolError("승인한 서식 파일이 바뀌었거나 없어졌습니다. 다시 실행해 주세요.")
    db.set_artifact_state(form_row["id"], "final")
    db.set_artifact_state(params["inputs"]["request_draft"]["artifact_id"], "reviewed")
    return {"final": form_row["id"]}


REGISTRY.register(Tool("request_draft", "1", policy.DRAFT, run_request, label="요청문 초안"))
REGISTRY.register(Tool("finalize_form", "1", policy.CHANGE, run_finalize, label="서식 배포본으로 확정",
                       scope=finalize_scope))
