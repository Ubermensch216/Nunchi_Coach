"""V1 — 결과 검증. 실행이 낸 산출물이 이 제품의 약속을 지키는지 다시 본다.

도구가 스스로 한 말을 믿지 않고 저장된 파일을 다시 열어 본다.

실패로 치는 것 (하나라도 있으면 실행을 멈추고 승인을 묻지 않는다)
  · 파일이 원장의 지문과 다르다 — 만든 뒤 바뀌었거나 없어졌다
  · 올해 공식 기준이 확인되지 않았는데 사전조사 표기가 없다
  · 올해 공식 근거가 없는데 올해 날짜가 값으로 실려 있다 (만든 날 제외)
  · 산출물의 모드가 서로 다르다

알리기만 하는 것 (경고)
  · 근거로 삼은 문서가 그 사이 바뀌었다 — 근거 서랍에서 '원문이 바뀜'으로 보인다
  · 개인정보 꼴이 나왔다 — 종류와 건수만 적는다. 값은 적지 않는다
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import openpyxl

from ...core import pii
from .. import artifacts as ledger, policy
from ..registry import REGISTRY, Tool, ToolError
from . import common
from .form import NOT_OFFICIAL

RESULT_TITLE = "실행 검증 결과"


def artifact_text(row) -> str:
    """산출물에 적힌 글 전체. 표는 칸을 이어 붙인다."""
    data = Path(row["path"]).read_bytes()
    if row["format"] == "xlsx":
        workbook = openpyxl.load_workbook(io.BytesIO(data))
        return "\n".join(str(c.value) for sheet in workbook.worksheets for line in sheet.iter_rows()
                         for c in line if isinstance(c.value, str) and not c.value.startswith("="))
    return data.decode("utf-8", errors="replace")


def _date_pattern(year: int) -> re.Pattern:
    return re.compile(rf"(?<!\d){year}\s*[-./년]\s*(\d{{1,2}})\s*[-./월]\s*(\d{{1,2}})")


def unfounded_dates(text: str, year: int, allowed: set[str]) -> list[str]:
    """올해 연도가 붙은 날짜 가운데 허용한 날을 뺀 것."""
    found = []
    for match in _date_pattern(year).finditer(text):
        iso = f"{year}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
        if iso not in allowed:
            found.append(iso)
    return found


def run_verify(context, params: dict) -> dict:
    db, inputs = context.db, params["inputs"]
    classified = inputs["classify"]["target"]
    year, official = classified["year"], classified["basis"] == common.OFFICIAL
    ids = (list(inputs["compare"]["artifacts"].values()) + list(inputs["form"]["artifacts"].values())
           + [inputs["checklist"]["artifact_id"], inputs["checklist"]["view_id"]])
    rows = [db.artifact(i) for i in ids]
    failures, warnings, personal = [], [], {}

    allowed = {context.today.isoformat()}
    if classified.get("due"):
        allowed.add(classified["due"])
    modes = {row["mode"] for row in rows}
    if len(modes) > 1:
        failures.append("산출물의 모드가 서로 다릅니다")
    evidence = db.evidence_for("artifact", ids)
    for row in rows:
        label = row["title"]
        if not ledger.current(db, row):
            failures.append(f"{label}: 파일이 만든 뒤 바뀌었거나 없어졌습니다")
            continue
        text = artifact_text(row)
        personal = pii.merge(personal, pii.scan(text))
        if not official:
            stray = unfounded_dates(text, year, allowed)
            if stray:
                failures.append(f"{label}: 올해 공식 근거가 없는데 {year}년 날짜 {len(stray)}개가 실려 있습니다")
            marked = (NOT_OFFICIAL in text) if row["kind"] in ("collection_form", "form_guide") \
                else ("미확인" in text or "확인 필요" in text or "초안" in text)
            if row["kind"] in ("collection_form", "form_guide", "comparison", "open_questions", "checklist_view") \
                    and not marked:
                failures.append(f"{label}: 올해 공식 기준 미확인 표기가 없습니다")
        stale = [e for e in evidence.get(row["id"], []) if not e["current"]]
        if stale:
            warnings.append(f"{label}: 근거 {len(stale)}건의 원문이 그 사이 바뀌었거나 보이지 않습니다")
    if personal:
        warnings.append(f"개인정보 꼴이 있습니다 — {pii.describe(personal)}. 값은 적지 않았습니다")

    lines = [f"# {RESULT_TITLE}", "", f"검증한 산출물 {len(rows)}건 · 모드 {'/'.join(sorted(modes))} · "
             f"대상 연도 {year}년 ({'공식 기준 확인됨' if official else '공식 기준 미확인'})", "",
             "## 실패", *(f"- {f}" for f in failures or ["없음"]), "",
             "## 경고", *(f"- {w}" for w in warnings or ["없음"]), "",
             "## 한계", "- 서식의 수식은 계산 결과를 확인하지 못했습니다.",
             "- 개인정보 점검은 주민등록번호·전화번호·전자우편 꼴만 찾습니다.",
             "- 비교의 ‘같음’은 칸의 글자가 같다는 뜻이며 의미가 같다는 보증이 아닙니다.", ""]
    report_id = context.artifacts.save("run_check", RESULT_TITLE, f"{year}_실행_검증결과.md",
                                       "\n".join(lines).encode("utf-8"),
                                       mode="official" if official else "preliminary", basis_year=year)
    if failures:
        raise ToolError("검증에서 걸렸습니다: " + "; ".join(failures))
    return {"artifact_id": report_id, "failures": 0, "warnings": len(warnings),
            "pii": sum(personal.values()), "checked": len(rows)}


REGISTRY.register(Tool("verify", "1", policy.DRAFT, run_verify, label="결과 검증"))
