"""B1~B4 — 부서별 실적 취합 서식 만들기 (계획서 2단계).

빈 표가 아니라 입력할 수 있고 점검되는 업무용 서식을 만든다. 시트는 일곱이다.

    01_작성안내   모드·근거 문서·작성 방법
    02_평가지표   지표별 정의·산식·증빙요건과 적용 연도
    03_부서별실적 실적 한 건이 한 줄. 목록·날짜 검사와 줄마다 점검 열
    04_증빙관리   실적과 증빙의 연결
    05_취합현황   부서별 건수·누락·진행률 (수식)
    06_오류점검   점검 결과 집계 (수식)
    07_변경이력   서식을 만든 기록

모드 두 가지
  preliminary  올해 공식 기준이 확인되지 않았을 때. 과거 지표를 참고로 싣고, 서식 안에
               '공식 평가기준이 아님'을 박는다. 전년도 지표를 올해 지표로 안내하는 일을 막는다.
  official     올해 공식 지표가 확인되었을 때. 02 시트를 올해 지표로 채운다.

전년도에 쓴 취합 서식이 있으면 그 열을 그대로 잇는다. 부서들이 해마다 같은 모양으로
적어 온 것을 바꾸는 것이 서식의 첫째 위험이기 때문이다.

만든 파일은 저장하기 전에 다시 열어 점검한다. **수식의 계산 결과는 확인하지 못한다**
(openpyxl은 계산하지 않는다) — 표현이 맞는지, 참조하는 시트와 범위가 있는지까지만 본다.
"""

from __future__ import annotations

import io
import json
import re

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from ...core import docrole, pii, yearcompare as yc
from .. import policy
from ..registry import REGISTRY, ToolError, Tool
from . import common

FORM_TITLE, GUIDE_TITLE, CHECK_TITLE = "부서별 실적취합 서식", "실적취합 작성안내", "서식 생성·검증 결과"
SHEETS = ("01_작성안내", "02_평가지표", "03_부서별실적", "04_증빙관리", "05_취합현황", "06_오류점검",
          "07_변경이력")
ROWS = 200                                  # 03 시트에 미리 깔아 두는 입력 줄 수
EXTRA_DEPARTMENT_ROWS = 8
DEFAULT_COLUMNS = ["부서", "구분", "추진내용", "추진일", "증빙자료"]
NOT_OFFICIAL = "공식 평가기준이 아님"
CHECK_CATEGORIES = ("필수값 누락", "증빙 누락", "날짜 형식 확인", "중복 의심")
STATUSES = ("미검증", "확인", "보완 필요")
MIN_DATE_SERIAL = 36526                     # 2000-01-01

_BOLD = Font(bold=True)
_HEAD_FILL = PatternFill("solid", fgColor="E8EEF7")
_WARN = Font(bold=True, color="B42318")


def column_roles(columns: list[str]) -> dict[str, int]:
    """열 이름에서 부서·구분·내용·날짜·증빙 열을 찾는다. 없는 것은 빠진다."""
    rules = (("proof", "증빙"), ("dept", "부서"), ("type", "구분|유형|지표|분야"),
             ("content", "내용|과제|사업명"), ("date", "일$|일자|기간|시기"))
    found: dict[str, int] = {}
    for role, pattern in rules:
        for index, name in enumerate(columns):
            if index not in found.values() and re.search(pattern, name):
                found[role] = index
                break
    return found


def previous_schema(db, mat: yc.Materials, before: int) -> tuple[list[str], int | None]:
    """전년도 취합 서식의 열. (열 이름들, 문서). 없으면 ([], None)."""
    for year in sorted((y for y in mat.by_year if y < before), reverse=True):
        for doc_id in mat.docs(year, docrole.FORM):
            for section in db.con.execute(
                    "SELECT kind, meta FROM document_sections WHERE doc_id = ? ORDER BY ordinal", (doc_id,)):
                header = json.loads(section["meta"] or "{}").get("header")
                if section["kind"] == "sheet" and header:
                    columns = [c.strip() for c in header.split("\t") if c.strip()]
                    if len(columns) >= 3 and {"dept", "content"} <= set(column_roles(columns)):
                        return columns, doc_id
    return [], None


def reference_indicators(db, mat: yc.Materials, target: common.Target):
    """02 시트에 실을 지표. (행들, 적용 연도, 모드). 올해 공식 지표를 읽었으면 공식이다."""
    if target.official:
        rows, _ = yc.read_indicators(db, target.criteria_docs)
        if rows:
            return list(rows.values()), target.year, "official"
    for year in sorted((y for y in mat.by_year if y < target.year), reverse=True):
        rows, _ = yc.read_indicators(db, mat.docs(year, docrole.CRITERIA))
        if rows:
            return list(rows.values()), year, "preliminary"
    return [], None, "preliminary"


def _header(sheet, row: int = 1) -> None:
    for cell in sheet[row]:
        if cell.value is not None:
            cell.font, cell.fill = _BOLD, _HEAD_FILL
            cell.alignment = Alignment(wrap_text=True, vertical="top")


def _widths(sheet, widths: list[int]) -> None:
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def build_workbook(db, name: str, today, mode: str, target_year: int, columns: list[str],
                   indicator_rows, applies_to, departments: list[str], used_docs: list[int],
                   past_years: list[int], run_id: int) -> bytes:
    official = mode == "official"
    schema = list(columns or DEFAULT_COLUMNS)
    roles = column_roles(schema)
    for role, default in (("dept", "부서"), ("content", "추진내용"), ("date", "추진일"), ("proof", "증빙자료")):
        if role not in roles:
            schema.append(default)
            roles = column_roles(schema)
    workbook = openpyxl.Workbook()
    guide = workbook.active
    guide.title = SHEETS[0]
    sheets = [guide] + [workbook.create_sheet(title) for title in SHEETS[1:]]
    _, indicators_sheet, records, proofs, status_sheet, errors, history = sheets

    # ── 01 작성안내 ──
    rows = [(f"{target_year}년 {name} 실적 취합 서식", _BOLD),
            ("상태: " + ("공식 평가기준 반영" if official else "초안 · 사전조사용"), _BOLD)]
    if not official:
        rows += [(f"※ {NOT_OFFICIAL} — {', '.join(f'{y}년' for y in past_years) or '과거'} 자료를 참고한 "
                  "사전조사 항목입니다. 올해 공식 평가계획·지표를 확인한 뒤 서식을 다시 만들어 주세요. (확인 필요)", _WARN)]
    rows += [("", None), ("근거 문서", _BOLD)]
    rows += [(f"- {common.filename(db, d)}", None) for d in used_docs] or [("- (근거로 삼을 서식·지표 문서를 찾지 못해 기본 열로 만들었습니다)", None)]
    rows += [("", None), ("작성 방법", _BOLD),
             ("1. 03_부서별실적에 실적 한 건을 한 줄로 적습니다. 실적 ID와 점검 열은 자동으로 채워집니다.", None),
             ("2. 증빙은 04_증빙관리에 실적 ID와 함께 적습니다.", None),
             ("3. 검증상태는 취합 담당자가 확인한 뒤에만 ‘확인’으로 바꿉니다. 실적 인정 여부는 이 서식이 정하지 않습니다.", None),
             ("4. 05_취합현황과 06_오류점검은 수식입니다. 직접 고치지 마세요.", None),
             ("5. 이 파일의 수식은 만든 시점에 계산 결과를 확인하지 못했습니다. 엑셀·한셀에서 열어 점검 열과 05·06 시트가 맞게 나오는지 확인해 주세요.", None),
             ("6. 개인정보(주민등록번호·전화번호 등)는 적지 않습니다.", None)]
    for text, font in rows:
        guide.append([text])
        if font:
            guide.cell(row=guide.max_row, column=1).font = font
    guide.column_dimensions["A"].width = 120

    # ── 02 평가지표 ──
    label = f"{target_year}년 공식" if official else f"참고 ({applies_to}년 기준 · 올해 미확인)" if applies_to else "참고"
    indicators_sheet.append(["지표", "배점", "정의", "산식", "증빙", "적용 연도", "구분", "출처"])
    for row in indicator_rows:
        indicators_sheet.append([row.name, int(row.score) if row.score.isdigit() else row.score, row.definition, row.formula, row.proof,
                                 applies_to, label, f"{common.filename(db, row.doc_id)} — {row.locator}"])
    if not indicator_rows:
        indicators_sheet.append(["(지표 표를 읽지 못했습니다)", None, None, None, None, None, label, ""])
    _header(indicators_sheet)
    _widths(indicators_sheet, [28, 8, 30, 30, 24, 10, 28, 40])
    indicator_last = max(2, indicators_sheet.max_row)

    # ── 03 부서별실적 ──
    head = ["실적 ID"] + schema + ["검증상태", "점검"]
    records.append(head)
    first, last = 2, ROWS + 1
    letters = {role: get_column_letter(index + 2) for role, index in roles.items()}
    schema_first, schema_last = get_column_letter(2), get_column_letter(len(schema) + 1)
    status_col, check_col = get_column_letter(len(schema) + 2), get_column_letter(len(schema) + 3)
    rng = lambda role: f"${letters[role]}${first}:${letters[role]}${last}"
    for row in range(first, last + 1):
        records.cell(row=row, column=1, value=(
            f'=IF(COUNTA({schema_first}{row}:{schema_last}{row})=0,"","R{target_year}-"&TEXT(ROW()-1,"000"))'))
        records.cell(row=row, column=len(schema) + 2, value=None)
        d, c, t, p = (f"{letters[r]}{row}" for r in ("dept", "content", "date", "proof"))
        records.cell(row=row, column=len(schema) + 3, value=(
            f'=IF(COUNTA({schema_first}{row}:{schema_last}{row})=0,"",'
            f'IF(OR({d}="",{c}="",{t}=""),"필수값 누락",'
            f'IF({p}="","증빙 누락",'
            f'IF(NOT(ISNUMBER({t})),"날짜 형식 확인",'
            f'IF(COUNTIFS({rng("dept")},{d},{rng("content")},{c})>1,"중복 의심","")))))'))
        records[f"{letters['date']}{row}"].number_format = "yyyy-mm-dd"
    _header(records)
    records.freeze_panes = "B2"
    _widths(records, [12] + [18] * len(schema) + [12, 14])

    dates = DataValidation(type="date", operator="greaterThan", formula1=str(MIN_DATE_SERIAL),
                           allow_blank=True, errorTitle="날짜", error="날짜 형식(예: 2026-05-20)으로 적어 주세요.")
    dates.add(f"{letters['date']}{first}:{letters['date']}{last}")
    statuses = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=True)
    statuses.add(f"{status_col}{first}:{status_col}{last}")
    records.add_data_validation(dates)
    records.add_data_validation(statuses)
    if "type" in letters and indicator_rows:
        kinds = DataValidation(type="list", formula1=f"'{SHEETS[1]}'!$A$2:$A${indicator_last}",
                               allow_blank=True, errorStyle="warning", errorTitle="구분",
                               error="지표 목록에 없는 구분입니다. 그래도 적으시겠습니까?")
        kinds.add(f"{letters['type']}{first}:{letters['type']}{last}")
        records.add_data_validation(kinds)

    # ── 04 증빙관리 ──
    proofs.append(["실적 ID", "증빙 문서(파일명·문서번호)", "증빙 종류", "확인 상태", "비고"])
    _header(proofs)
    proofs.freeze_panes = "A2"
    _widths(proofs, [12, 40, 18, 14, 40])
    confirm = DataValidation(type="list", formula1='"미확인,확인,보완 필요"', allow_blank=True)
    confirm.add(f"D2:D{ROWS + 1}")
    proofs.add_data_validation(confirm)

    # ── 05 취합현황 ──
    status_sheet.append(["부서", "실적 건수", "증빙 누락", "검증 완료", "진행률"])
    sheet3 = f"'{SHEETS[2]}'!"
    for index in range(len(departments) + EXTRA_DEPARTMENT_ROWS):
        row = index + 2
        status_sheet.append([departments[index] if index < len(departments) else None,
                             f'=IF(A{row}="","",COUNTIF({sheet3}{rng("dept")},A{row}))',
                             f'=IF(A{row}="","",COUNTIFS({sheet3}{rng("dept")},A{row},{sheet3}${check_col}${first}:${check_col}${last},"증빙 누락"))',
                             f'=IF(A{row}="","",COUNTIFS({sheet3}{rng("dept")},A{row},{sheet3}${status_col}${first}:${status_col}${last},"확인"))',
                             f'=IF(OR(A{row}="",B{row}=0),"",D{row}/B{row})'])
        status_sheet[f"E{row}"].number_format = "0%"
    _header(status_sheet)
    status_sheet.append([])
    status_sheet.append([f"※ 위 부서 목록은 {past_years[-1]}년 실적표에 나온 부서입니다(참고). 올해 취합 대상 부서로 고쳐 주세요."
                         if departments and past_years else "※ 취합 대상 부서를 직접 적어 주세요."])
    _widths(status_sheet, [24, 12, 12, 12, 10])

    # ── 06 오류점검 ──
    errors.append(["점검", "건수", "설명"])
    errors.append(["입력된 실적", f'=COUNTIF({sheet3}$A${first}:$A${last},"?*")', "실적 ID가 붙은 줄"])
    explain = {"필수값 누락": "부서·추진내용·추진일 중 빈 칸", "증빙 누락": "증빙자료 칸이 빈 실적",
               "날짜 형식 확인": "추진일이 날짜가 아닌 칸", "중복 의심": "같은 부서가 같은 내용을 두 번 적은 줄 (삭제하지 않고 확인)"}
    for category in CHECK_CATEGORIES:
        errors.append([category, f'=COUNTIF({sheet3}${check_col}${first}:${check_col}${last},"{category}")',
                       explain[category]])
    errors.append(["점검 항목 합계", f"=SUM(B3:B{2 + len(CHECK_CATEGORIES)})", "0이어도 실적 인정을 뜻하지 않습니다"])
    _header(errors)
    _widths(errors, [20, 10, 60])

    # ── 07 변경이력 ──
    history.append(["판", "만든 날", "모드", "근거 문서", "실행 번호", "사용자 확인"])
    history.append([1, today.isoformat(), "공식 평가기준 반영" if official else "초안 · 사전조사용",
                    ", ".join(common.filename(db, d) for d in used_docs), run_id, None])
    _header(history)
    _widths(history, [6, 12, 22, 60, 10, 20])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def check_workbook(data: bytes, mode: str, expected_columns: list[str], indicator_count: int) -> list[dict]:
    """저장하기 전에 다시 열어 구조를 점검한다. 수식의 계산 결과는 확인하지 못한다."""
    checks: list[dict] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    workbook = openpyxl.load_workbook(io.BytesIO(data))
    add("시트 구성", tuple(workbook.sheetnames) == SHEETS, ", ".join(workbook.sheetnames))
    if tuple(workbook.sheetnames) != SHEETS:
        return checks
    records = workbook[SHEETS[2]]
    header = [c.value for c in records[1]]
    roles = column_roles(header[1:-2])
    add("필수 열", {"dept", "content", "date", "proof"} <= set(roles), " | ".join(map(str, header)))
    if expected_columns:
        add("전년도 서식의 열 유지", header[1:1 + len(expected_columns)] == expected_columns, "")
    kinds = [dv.type for dv in records.data_validations.dataValidation]
    add("입력 검사(날짜·목록)", "date" in kinds and kinds.count("list") >= 1, f"{len(kinds)}개")
    add("입력 줄 수", records.max_row == ROWS + 1, f"{records.max_row - 1}줄")
    indicator_rows = [r for r in workbook[SHEETS[1]].iter_rows(min_row=2, values_only=True) if r[0]]
    add("지표 행", len(indicator_rows) == max(indicator_count, 1), f"{len(indicator_rows)}행")

    formulas, bad_refs, unbalanced = 0, [], 0
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    formulas += 1
                    if cell.value.count("(") != cell.value.count(")"):
                        unbalanced += 1
                    for ref in re.findall(r"'([^']+)'!", cell.value):
                        if ref not in SHEETS:
                            bad_refs.append(ref)
    add("수식 괄호", unbalanced == 0, f"수식 {formulas}개 · 짝이 안 맞는 것 {unbalanced}개")
    add("수식이 가리키는 시트", not bad_refs, ", ".join(sorted(set(bad_refs))))
    text = " ".join(str(c.value) for c in workbook[SHEETS[0]]["A"] if c.value)
    if mode == "preliminary":
        add("사전조사용 표기", NOT_OFFICIAL in text and "확인 필요" in text, "")
    else:
        add("공식 모드에는 사전조사용 표기가 없음", NOT_OFFICIAL not in text, "")
    return checks


def render_guide(db, name: str, today, mode: str, target_year: int, columns: list[str], schema_doc,
                 applies_to, used_docs: list[int], past_years: list[int]) -> str:
    official = mode == "official"
    out = [f"# {target_year}년 {name} 실적 취합 — 작성 안내", "",
           f"만든 날 {today.isoformat()} · 상태 **{'공식 평가기준 반영' if official else '초안 · 사전조사용'}**", ""]
    if not official:
        out += [f"> **{NOT_OFFICIAL}.** {', '.join(f'{y}년' for y in past_years) or '과거'} 자료를 참고한 사전조사 항목입니다. "
                f"{target_year}년 공식 평가계획·지표를 확인하는 대로 서식을 다시 만들어 주세요. (확인 필요)", ""]
    out += ["## 서식의 열",
            (f"전년도 서식({common.filename(db, schema_doc)})의 열을 그대로 이었습니다: " + ", ".join(columns))
            if schema_doc else "전년도 서식을 찾지 못해 기본 열로 만들었습니다: " + ", ".join(columns or DEFAULT_COLUMNS), "",
            "## 지표", (f"02_평가지표에 {applies_to}년 지표를 " + ("올해 공식 지표로" if official else "참고로") + " 실었습니다.")
            if applies_to else "지표 표를 읽지 못해 02_평가지표를 비워 두었습니다.", "",
            "## 근거 문서", *(f"- {common.filename(db, d)}" for d in used_docs), "",
            "## 한계",
            "- 수식의 계산 결과는 만든 시점에 확인하지 못했습니다. 열어서 점검 열과 05·06 시트를 확인해 주세요.",
            "- 중복 의심은 같은 부서·같은 내용만 봅니다. 삭제하지 않고 표시만 합니다.",
            "- 개인정보 점검은 주민등록번호·전화번호·전자우편 꼴만 찾습니다.", ""]
    return "\n".join(out)


def run_form(context, params: dict) -> dict:
    db = context.db
    found = params["inputs"]["discover"]
    name = common.task_name(db, found["task_id"])
    doc_ids = [item["id"] for item in found["included"]]
    mat = yc.materials(db, doc_ids)
    target = common.judge_target(db, context.case_id, doc_ids, params["target_year"], mat)

    columns, schema_doc = previous_schema(db, mat, target.year)
    rows, applies_to, mode = reference_indicators(db, mat, target)
    past_years = [y for y in mat.years(docrole.CRITERIA, docrole.FORM, docrole.PERFORMANCE) if y < target.year]
    departments: list[str] = []
    if past_years:
        departments = yc.summarize_performance(db, past_years[-1], mat).departments
    used = [d for d in ([schema_doc] if schema_doc else []) + list(dict.fromkeys(r.doc_id for r in rows)) if d]

    data = build_workbook(db, name, context.today, mode, target.year, columns, rows, applies_to,
                          departments, used, past_years, context.run_id)
    checks = check_workbook(data, mode, columns, len(rows))
    failed = [c for c in checks if not c["passed"]]
    if failed:
        raise ToolError("서식을 점검해 보니 맞지 않는 곳이 있어 저장하지 않았습니다: "
                        + "; ".join(f"{c['name']}" for c in failed))
    leaked = pii.scan(" ".join(str(c.value) for sheet in openpyxl.load_workbook(io.BytesIO(data)).worksheets
                               for row in sheet.iter_rows() for c in row if isinstance(c.value, str)))

    snapshot = common.sources_of(db, used)
    stem = f"{target.year}_{name}_실적취합_{'공식' if mode == 'official' else '초안'}"
    refs = [yc.Ref(r.doc_id, r.locator, r.quote) for r in rows]
    form_id = context.artifacts.save("collection_form", FORM_TITLE, stem + ".xlsx", data, mode=mode,
                                     basis_year=target.year, sources=snapshot,
                                     evidence=common.evidence_dicts(refs))
    guide = render_guide(db, name, context.today, mode, target.year, columns, schema_doc, applies_to, used, past_years)
    guide_id = context.artifacts.save("form_guide", GUIDE_TITLE, stem + "_작성안내.md", guide.encode("utf-8"),
                                      mode=mode, basis_year=target.year, sources=snapshot)
    report = {"mode": mode, "target_year": target.year, "checks": checks,
              "limits": ["수식의 계산 결과는 확인하지 못했습니다", "개인정보는 주민등록번호·전화번호·전자우편 꼴만 찾습니다"],
              "personal_data_found": pii.describe(leaked)}
    check_id = context.artifacts.save("form_check", CHECK_TITLE, "서식_생성및검증_결과.json",
                                      json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
                                      mode=mode, basis_year=target.year, sources=snapshot)
    return {"artifacts": {"form": form_id, "guide": guide_id, "check": check_id}, "mode": mode,
            "columns": len(columns or DEFAULT_COLUMNS), "indicator_rows": len(rows),
            "schema_from": schema_doc, "checks": len(checks), "pii": sum(leaked.values())}


REGISTRY.register(Tool("form", "1", policy.DRAFT, run_form, label="취합 서식 만들기"))
