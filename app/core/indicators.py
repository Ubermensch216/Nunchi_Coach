"""지표 표 읽기 — 표에서 지표 행을 구조로 꺼낸다 (행동형 업무 비서 계획서 M4).

두 해의 평가 기준을 견주려면 글을 요약해서는 안 된다. 같은 모양에 담아 칸끼리
견줘야 한다 — 지표 이름이 같아도 배점이나 산식이 달라졌을 수 있고, 그것이 이
업무에서 가장 중요한 변화다.

파서가 남긴 표의 구조(머리 행·행 번호)를 쓴다. 머리글이 이름 칸과 그 밖의 칸
하나 이상을 알려 줄 때만 읽는다. 맞지 않으면 읽지 않는다 — 아무 표나 지표로
읽으면 견줄 수 없는 것을 견주게 된다.

읽는 표의 꼴
  · HWPX·ODT   "첫 행: 지표 | 배점\\n1열: 규제 발굴 | 2열: 30"  (행마다 한 조각)
  · XLSX       탭으로 나뉜 줄들, 구조 정보에 머리 행                (시트 조각)
그 밖의 꼴(DOCX·PDF의 표)은 아직 읽지 않는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

NAME, SCORE, DEFINITION, FORMULA, PROOF = "name", "score", "definition", "formula", "proof"
FIELDS = (NAME, SCORE, DEFINITION, FORMULA, PROOF)
FIELD_LABEL = {NAME: "지표", SCORE: "배점", DEFINITION: "정의", FORMULA: "산식", PROOF: "증빙"}

# 머리글 낱말 → 칸의 뜻. 앞의 것이 이긴다("평가방법"은 산식이지 지표가 아니다).
_HEADER_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (SCORE, re.compile(r"배점|만점|가중치")),
    (FORMULA, re.compile(r"산식|산출\s*식|산출\s*방법|평가\s*방법|측정\s*방법")),
    (PROOF, re.compile(r"증빙|근거\s*자료|제출\s*자료|확인\s*자료")),
    (DEFINITION, re.compile(r"정의|설명|평가\s*내용|세부\s*내용|내용")),
    (NAME, re.compile(r"지표|평가\s*항목|세부\s*항목|항목")),
)
_HEAD_LINE = re.compile(r"^(?:첫 행|머리글):\s*(.+)$")
_CELL = re.compile(r"(\d+)열:\s*(.*?)(?=\s\|\s\d+열:|$)")
_SPACE = re.compile(r"\s+")
_NUMBERING = re.compile(r"^\s*(?:\d+(?:-\d+)?[.)]|[①-⑳]|[가-하][.)])\s*")


@dataclass(frozen=True)
class Indicator:
    name: str
    score: str
    definition: str
    formula: str
    proof: str
    doc_id: int
    locator: str
    quote: str           # 이 행이 적힌 원문

    def key(self) -> str:
        """이름을 견줄 때 쓰는 꼴. 공백·기호·번호를 걷는다."""
        return normalize(self.name)


def normalize(name: str) -> str:
    text = _NUMBERING.sub("", name)
    return re.sub(r"[\s·.,()\-_/]", "", text).lower()


def columns(headers: list[str]) -> dict[str, int] | None:
    """머리글에서 칸의 뜻을 읽는다. 이름 칸과 다른 칸 하나가 없으면 None."""
    found: dict[str, int] = {}
    for index, header in enumerate(headers):
        for field_name, pattern in _HEADER_RULES:
            if pattern.search(header):
                found.setdefault(field_name, index)
                break
    if NAME not in found or len(found) < 2:
        return None
    return found


def read_document(db, doc_id: int) -> list[Indicator]:
    """문서의 지표 행. 지표 표가 없으면 빈 목록이다."""
    out: list[Indicator] = []
    for section in db.con.execute(
            "SELECT kind, locator, text, meta FROM document_sections WHERE doc_id = ? "
            "ORDER BY ordinal", (doc_id,)):
        meta = json.loads(section["meta"] or "{}")
        if section["kind"] == "sheet":
            rows = _sheet_rows(section["text"], meta.get("header"))
        else:
            rows = _table_row(section["text"])
        if not rows:
            continue
        headers, lines = rows
        places = columns(headers)
        if places is None:
            continue
        for values, quote in lines:
            def cell(field_name: str) -> str:
                index = places.get(field_name)
                return _clean(values[index]) if index is not None and index < len(values) else ""
            if not cell(NAME) or cell(NAME) == _clean(headers[places[NAME]]):
                continue
            out.append(Indicator(cell(NAME), cell(SCORE), cell(DEFINITION), cell(FORMULA),
                                 cell(PROOF), doc_id, section["locator"], quote))
    return out


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value or "").strip()


def _table_row(text: str):
    """HWPX·ODT의 행 조각 → (머리글, [(값들, 원문)])."""
    headers, cells = None, None
    for line in text.splitlines():
        head = _HEAD_LINE.match(line.strip())
        if head:
            headers = [part.strip() for part in head.group(1).split(" | ")]
        elif line.lstrip().startswith("1열:"):
            cells = [value for _number, value in _CELL.findall(line.strip())]
            quote = line.strip()
    if not headers or not cells:
        return None
    return headers, [(cells, quote)]


def _sheet_rows(text: str, header: str | None):
    """XLSX 시트 조각 → (머리글, [(값들, 원문)])."""
    if not header:
        return None
    headers = header.split("\t")
    lines = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("[") or line == header:
            continue
        lines.append((line.split("\t"), _clean(line.replace("\t", " | "))))
    return headers, lines
