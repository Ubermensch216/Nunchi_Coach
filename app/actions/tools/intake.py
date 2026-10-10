"""H3·H4·H5 — 회신 취합: 읽기·점검, 증빙 대조, 취합본과 보고서 초안 (계획서 4단계).

    read_replies       사용자가 고른 폴더의 회신 엑셀을 읽어 줄마다 점검한다
    check_evidence     증빙칸의 항목마다 파일을 찾고 실적 내용과 낱말이 겹치는지 본다
    collection_report  취합본(엑셀)과 실적 보고서 초안(글·한글 문서)을 만든다

지키는 것
  · 회신 파일은 읽기만 한다. 취합본은 **새 파일**이다.
  · 중복 의심은 묶음 번호만 달고 두 줄을 모두 남긴다. 어떤 줄도 지우거나 합치지 않는다.
  · ‘인정’이라는 말을 쓰지 않는다. 점검은 기계적으로 볼 수 있는 것이고, 증빙이 실적을
    뒷받침하는지는 담당자가 정한다. 검증상태 열은 ‘미검증’으로 시작한다.
  · 개인정보 꼴은 종류와 건수만 적는다.
  · 다시 실행하면 새 판이 만들어지고 이전 판은 남는다 — 회신이 더 들어올 때마다 돌릴 수 있다.
"""

from __future__ import annotations

import io
import json
from collections import Counter
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ...core import pii
from ...studio import render, templates
from .. import artifacts as ledger, policy, replies
from ..registry import REGISTRY, Blocked, Tool, ToolError
from . import common, followup
from .request import PRELIMINARY_NOTE

DATA_TITLE, EVIDENCE_TITLE = "회신 취합 데이터", "증빙 대조 결과"
BOOK_TITLE, REPORT_TITLE, HWPX_TITLE = "부서별 실적 취합본", "실적 보고서 초안", "실적 보고서 초안 (한글 문서)"
SHEETS = ("안내", "취합", "부서 현황", "점검 목록", "중복 의심", "파일 점검")
LIMITS = ("이 점검은 기계적으로 볼 수 있는 것(빈 칸, 날짜 형식, 중복 의심, 증빙 파일의 유무와 낱말 겹침)뿐입니다.",
          "실적을 인정할지, 증빙이 실적을 뒷받침하는지는 담당자가 정합니다. 검증상태는 ‘미검증’으로 시작합니다.",
          "개인정보는 주민등록번호·전화번호·전자우편 꼴만 찾습니다.")
_HEAD = Font(bold=True)
_FILL = PatternFill("solid", fgColor="E8EEF7")


def _folder(params: dict) -> Path:
    folder = Path(params.get("folder") or "")
    if not params.get("folder") or not folder.is_dir():
        raise Blocked("회신 폴더를 찾을 수 없습니다. 부서 회신 파일이 들어 있는 폴더를 고른 뒤 다시 실행해 주세요.")
    return folder


def _load(db, artifact_id: int) -> dict:
    row = db.artifact(artifact_id)
    if row is None or not ledger.current(db, row):
        raise ToolError("앞 단계의 결과 파일이 바뀌었거나 없어졌습니다. 다시 실행해 주세요.")
    return json.loads(Path(row["path"]).read_text(encoding="utf-8"))


def _save_json(context, kind: str, title: str, name: str, payload: dict, mode: str, year: int) -> int:
    data = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
    return context.artifacts.save(kind, title, name, data, mode=mode, basis_year=year)


def run_read(context, params: dict) -> dict:
    db = context.db
    folder = _folder(params)
    inputs = followup.case_inputs(context)
    form_row = followup.form_artifact(db, inputs.case["id"])
    files, rows = replies.read_folder(folder, context.today, followup.type_names(form_row))
    if not files:
        raise Blocked("이 폴더에 회신 엑셀 파일(.xlsx)이 없습니다. 부서가 보낸 파일을 한 폴더에 모아 주세요.")
    if not rows:
        problems = "; ".join(f"{f.name}: {p}" for f in files for p in f.problems[:1])[:300]
        raise Blocked(f"읽을 수 있는 실적 줄이 하나도 없습니다. ({problems})")
    mode = "official" if inputs.target.official else "preliminary"
    artifact_id = _save_json(context, "collection_data", DATA_TITLE, "회신_취합_데이터.json", {
        "folder": folder.name, "target_year": inputs.target.year,
        "files": [vars(f) for f in files], "rows": replies.rows_to_json(rows)}, mode, inputs.target.year)
    issues = Counter(issue for row in rows for issue in row.issues)
    return {"artifact_id": artifact_id, "files": len(files), "problem_files": sum(1 for f in files if f.problems),
            "rows": len(rows), "issue_rows": sum(1 for r in rows if r.issues),
            "groups": len({r.group for r in rows if r.group}), "issues": dict(issues)}


def run_evidence(context, params: dict) -> dict:
    db = context.db
    folder = _folder(params)
    data = _load(db, params["inputs"]["read_replies"]["artifact_id"])
    rows = replies.rows_from_json(data["rows"])
    candidates = replies.proof_files(folder, skip={f["name"] for f in data["files"]})
    cache: dict = {}
    for row in rows:
        replies.match_row(row, candidates, cache)
    data["rows"] = replies.rows_to_json(rows)
    data["proof_files"] = len(candidates)
    artifact_id = _save_json(context, "evidence_check", EVIDENCE_TITLE, "증빙_대조_결과.json", data,
                             db.artifact(params["inputs"]["read_replies"]["artifact_id"])["mode"],
                             data["target_year"])
    counts = Counter(row.evidence for row in rows)
    return {"artifact_id": artifact_id, "proof_files": len(candidates), "by_status": dict(counts)}


# ── 취합본(엑셀) ────────────────────────────────────────────────────

def _sheet(book, title: str, header: list[str], widths: list[int]):
    sheet = book.create_sheet(title)
    sheet.append(header)
    for cell in sheet[1]:
        cell.font, cell.fill = _HEAD, _FILL
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.freeze_panes = "A2"
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    return sheet


def build_book(data: dict, rows: list[replies.Row], name: str, year: int, mode: str, today) -> bytes:
    book = openpyxl.Workbook()
    guide = book.active
    guide.title = SHEETS[0]
    for text in [f"{year}년 {name} 부서별 실적 취합본", f"만든 날 {today.isoformat()} · 회신 파일 {len(data['files'])}개 · 실적 {len(rows)}건",
                 PRELIMINARY_NOTE if mode == "preliminary" else "초안 — 공식 평가기준 반영.", "", "읽는 법", *LIMITS,
                 "이 파일은 회신 파일을 읽어 새로 만든 것입니다. 회신 원본은 바뀌지 않았습니다."]:
        guide.append([text])
    guide.column_dimensions["A"].width = 120
    guide["A1"].font = _HEAD

    table = _sheet(book, SHEETS[1], ["번호", "부서", "구분", "추진내용", "추진일", "증빙자료", "증빙 대조", "점검 결과",
                                     "중복 묶음", "파일", "행", "검증상태"], [6, 14, 16, 40, 12, 24, 30, 28, 8, 24, 6, 10])
    for number, row in enumerate(rows, 1):
        table.append([number, row.dept, row.kind, row.content, row.date or row.date_raw, row.proof,
                      replies.EVIDENCE_LABEL.get(row.evidence, ""), ", ".join(row.issues), row.group,
                      row.file, row.line, "미검증"])

    by_dept: dict[str, list[replies.Row]] = {}
    for row in rows:
        by_dept.setdefault(row.dept, []).append(row)
    status = _sheet(book, SHEETS[2], ["부서", "실적 건수", "점검 항목이 있는 줄", "증빙 누락", "증빙 파일을 찾지 못함",
                                      "중복 의심 줄", "개인정보 꼴 있는 줄", "담당자 확인 건수"], [20, 10, 14, 10, 14, 10, 14, 12])
    for dept in sorted(by_dept):
        group = by_dept[dept]
        status.append([dept, len(group), sum(1 for r in group if r.issues),
                       sum(1 for r in group if replies.NO_PROOF in r.issues),
                       sum(1 for r in group if r.evidence == "missing"),
                       sum(1 for r in group if replies.DUPLICATE in r.issues),
                       sum(1 for r in group if replies.PERSONAL in r.issues), 0])

    todo = _sheet(book, SHEETS[3], ["번호", "부서", "파일", "행", "점검 항목", "내용(앞부분)"], [6, 14, 24, 6, 20, 50])
    for number, row in enumerate(rows, 1):
        for issue in sorted(row.issues, key=replies.ISSUE_ORDER.index):
            detail = f"{issue} ({', '.join(row.missing)})" if issue == replies.MISSING else issue
            todo.append([number, row.dept, row.file, row.line, detail, row.content[:40]])

    dup = _sheet(book, SHEETS[4], ["묶음", "번호", "부서", "파일", "행", "내용(앞부분)"], [8, 6, 14, 24, 6, 50])
    for number, row in sorted(enumerate(rows, 1), key=lambda item: (item[1].group or 0, item[0])):
        if row.group:
            dup.append([row.group, number, row.dept, row.file, row.line, row.content[:40]])
    dup.append([])
    dup.append(["※ 같은 내용으로 보이는 줄입니다. 지우거나 합치지 않았습니다. 같은 과제를 여러 부서가 함께 냈는지 확인하세요."])

    files = _sheet(book, SHEETS[5], ["파일", "읽은 줄 수", "시트", "문제"], [36, 10, 16, 70])
    for item in data["files"]:
        files.append([item["name"], item["rows"], item["sheet"], "; ".join(item["problems"]) or "없음"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def check_book(data: bytes, expected_rows: int) -> list[str]:
    """저장하기 전에 다시 열어 점검한다. 문제 목록(비어 있으면 통과)."""
    problems = []
    book = openpyxl.load_workbook(io.BytesIO(data))
    if tuple(book.sheetnames) != SHEETS:
        problems.append("시트 구성이 다릅니다")
        return problems
    if book[SHEETS[1]].max_row - 1 != expected_rows:
        problems.append(f"취합 줄 수가 다릅니다 ({book[SHEETS[1]].max_row - 1} ≠ {expected_rows})")
    if any(r[11] != "미검증" for r in book[SHEETS[1]].iter_rows(min_row=2, values_only=True)):
        problems.append("검증상태가 ‘미검증’이 아닌 줄이 있습니다")
    return problems


# ── 보고서 초안 ─────────────────────────────────────────────────────

def render_report(data: dict, rows: list[replies.Row], name: str, year: int, mode: str, today) -> str:
    depts = Counter(row.dept for row in rows)
    kinds = Counter(row.kind or "(구분 비어 있음)" for row in rows)
    evidence = Counter(row.evidence or "none" for row in rows)
    issues = Counter(issue for row in rows for issue in row.issues)
    problem_files = [f for f in data["files"] if f["problems"]]
    out = [f"# {year}년 {name} 부서별 실적 보고서(안)", "",
           "> " + (PRELIMINARY_NOTE if mode == "preliminary" else "초안 — 실적의 인정 여부는 담당자가 정합니다."), "",
           "## 1. 개요",
           f"- 회신 파일 {len([f for f in data['files'] if f['rows']])}개에서 실적 {len(rows)}건을 읽었습니다 (작성 {today.isoformat()}).",
           f"- 실적을 낸 부서 {len(depts)}개",
           f"- 읽지 못했거나 문제가 있는 파일 {len(problem_files)}개", "",
           "## 2. 부서별 실적 현황", "| 부서 | 실적 건수 | 점검 항목이 있는 줄 |", "|---|---|---|",
           *(f"| {dept} | {count} | {sum(1 for r in rows if r.dept == dept and r.issues)} |"
             for dept, count in sorted(depts.items())), "",
           "## 3. 구분별 실적", "| 구분 | 건수 |", "|---|---|",
           *(f"| {kind} | {count} |" for kind, count in sorted(kinds.items())), "",
           "## 4. 증빙 현황 (파일 이름과 낱말 겹침으로 본 것이며, 뒷받침 여부는 담당자가 판단)",
           "| 증빙 대조 | 건수 |", "|---|---|",
           *(f"| {replies.EVIDENCE_LABEL[key]} | {evidence[key]} |" for key in replies.EVIDENCE_LABEL if evidence.get(key)), "",
           "## 5. 점검 결과", "| 점검 항목 | 줄 수 |", "|---|---|",
           *(f"| {issue} | {issues[issue]} |" for issue in replies.ISSUE_ORDER if issues.get(issue)),
           *([] if issues else ["| (점검에 걸린 줄 없음) | 0 |"]), "",
           "## 6. 확인 필요사항"]
    todo = []
    if issues.get(replies.MISSING):
        todo.append(f"필수 칸이 빈 줄 {issues[replies.MISSING]}건 — 해당 부서에 보완 요청")
    if issues.get(replies.NO_PROOF) or evidence.get("missing"):
        todo.append(f"증빙이 비었거나 파일을 찾지 못한 줄 {issues.get(replies.NO_PROOF, 0) + evidence.get('missing', 0)}건")
    if issues.get(replies.DUPLICATE):
        todo.append(f"중복 의심 {len({r.group for r in rows if r.group})}묶음 — 같은 과제를 여러 부서가 낸 것인지 확인")
    if issues.get(replies.PERSONAL):
        todo.append(f"개인정보 꼴이 있는 줄 {issues[replies.PERSONAL]}건 — 외부에 내기 전에 확인")
    todo += [f"{f['name']}: {f['problems'][0]}" for f in problem_files]
    if mode == "preliminary":
        todo.append(f"{year}년 공식 평가기준이 확인되지 않았습니다 — 구분(지표)과 증빙요건을 공식 기준으로 다시 맞추세요")
    out += [f"- {line}" for line in todo] or ["- 없음"]
    out += ["", *(f"> {line}" for line in LIMITS), ""]
    return "\n".join(out)


def run_report(context, params: dict) -> dict:
    db = context.db
    data = _load(db, params["inputs"]["check_evidence"]["artifact_id"])
    rows = replies.rows_from_json(data["rows"])
    inputs = followup.case_inputs(context)
    mode = "official" if inputs.target.official else "preliminary"
    year = inputs.target.year
    book = build_book(data, rows, inputs.name, year, mode, context.today)
    problems = check_book(book, len(rows))
    if problems:
        raise ToolError("취합본을 점검해 보니 맞지 않아 저장하지 않았습니다: " + "; ".join(problems))
    text = render_report(data, rows, inputs.name, year, mode, context.today)
    try:
        hwpx = render.to_hwpx(text, templates.DEFAULT_FORMAT, f"{year}년 {inputs.name} 부서별 실적 보고서(안)")
    except Exception as exc:
        raise ToolError(f"한글 문서 초안을 만들지 못했습니다 ({type(exc).__name__}).") from exc
    stem = f"{year}_{inputs.name}"
    save = context.artifacts.save
    book_id = save("collection_book", BOOK_TITLE, f"{stem}_부서별_실적_취합본.xlsx", book, mode=mode, basis_year=year)
    report_id = save("report_draft", REPORT_TITLE, f"{stem}_실적_보고서_초안.md", text.encode("utf-8"), mode=mode, basis_year=year)
    hwpx_id = save("report_draft_hwpx", HWPX_TITLE, f"{stem}_실적_보고서_초안.hwpx", hwpx, mode=mode, basis_year=year)
    leaked = pii.merge(*(row.personal for row in rows))
    return {"artifacts": {"book": book_id, "report": report_id, "hwpx": hwpx_id}, "rows": len(rows),
            "departments": len({r.dept for r in rows}), "pii": sum(leaked.values())}


REGISTRY.register(Tool("read_replies", "1", policy.DRAFT, run_read, label="회신 읽기·점검"))
REGISTRY.register(Tool("check_evidence", "1", policy.DRAFT, run_evidence, label="증빙 대조"))
REGISTRY.register(Tool("collection_report", "1", policy.DRAFT, run_report, label="취합본·보고서 초안"))
