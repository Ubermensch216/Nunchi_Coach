"""문서 서식 — 어떤 항목을 어떤 순서로, 어떤 모양으로 쓰는가.

서식 하나는 두 가지를 정한다.
  · 항목(sections): 문서의 뼈대. 초안을 엮을 때 항목마다 따로 쓴다.
  · 모양(format): 글꼴·크기·줄간격과 단계별 기호(□ → ○ → -). 파일로 내보낼 때 쓴다.

내장 서식은 코드에 있고 프로젝트 DB(doc_templates)에 한 번 심는다. 사용자가
고치거나 새로 만든 서식은 DB에만 있다 — 내장 서식을 고치면 사본이 생긴다.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

# 공문서에서 흔한 기본 모양. 글꼴은 한글·워드 어디에나 있는 것으로 둔다 —
# 없는 글꼴을 적으면 여는 PC마다 다른 글꼴로 바뀌어 줄 수가 달라진다.
DEFAULT_FORMAT = {
    "version": 1,
    "page": {"width_mm": 210, "height_mm": 297,
             "margin_mm": {"top": 20, "bottom": 15, "left": 20, "right": 20}},
    "title": {"font": "맑은 고딕", "size": 20, "bold": True, "align": "center", "line": 160},
    "heading": {"font": "맑은 고딕", "size": 15, "bold": True, "align": "left", "line": 160},
    "body": {"font": "맑은 고딕", "size": 12, "bold": False, "align": "justify", "line": 160},
    # 목록의 단계. lead는 줄 앞 공백 칸 수다(공문서는 들여쓰기를 칸으로 맞춘다).
    "levels": [
        {"glyph": "○", "font": "맑은 고딕", "size": 12, "bold": False, "lead": 1, "line": 160},
        {"glyph": "-", "font": "맑은 고딕", "size": 12, "bold": False, "lead": 3, "line": 160},
        {"glyph": "·", "font": "맑은 고딕", "size": 11, "bold": False, "lead": 5, "line": 160},
    ],
    "note": {"font": "맑은 고딕", "size": 11, "bold": False, "align": "left", "line": 150},
}


@dataclass(slots=True)
class Section:
    title: str
    instruction: str = ""


@dataclass(slots=True)
class Template:
    title: str
    doc_type: str
    description: str = ""
    sections: list[Section] = field(default_factory=list)
    guidance: str = ""
    format: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_FORMAT))
    key: str = ""
    builtin: bool = False
    id: int | None = None


def _sections(*pairs: tuple[str, str]) -> list[Section]:
    return [Section(title, instruction) for title, instruction in pairs]


BUILTIN = [
    Template("업무보고", "업무보고", "주요 현안·추진 실적·향후 계획을 보고하는 문서", _sections(
        ("1. 보고 배경 및 목적", "왜 보고하는지, 무엇에 관한 보고인지."),
        ("2. 주요 추진 실적 및 현황", "지금까지 한 일과 현재 상태. 수치·일자를 자료에 적힌 대로."),
        ("3. 당면 과제 및 문제점", "자료에 드러난 문제·미결 사항."),
        ("4. 향후 추진 계획 및 일정", "앞으로 할 일과 기한."),
        ("5. 건의 및 협조 요청사항", "다른 부서·상급자에게 요청할 것. 자료에 없으면 비운다.")),
        "객관적 사실과 수치 중심의 개조식(~함, ~음)으로 쓴다.", key="work-report", builtin=True),
    Template("기본 계획서", "기본 계획서", "사업·정책의 기본 방향과 추진 전략을 세우는 문서", _sections(
        ("1. 추진 배경 및 필요성", "왜 이 사업을 하는지."),
        ("2. 관련 현황 및 문제점 분석", "현재 상태와 문제. 수치는 자료에 적힌 대로."),
        ("3. 기본 방향 및 추진 목표", "무엇을 이루려는지."),
        ("4. 세부 추진 과제", "과제별 내용."),
        ("5. 소요 예산 및 재원", "금액·재원. 자료에 없으면 비운다."),
        ("6. 추진 일정 및 기대 효과", "단계별 일정과 기대 효과.")),
        "개조식으로 쓰고, 일정과 금액은 자료에 적힌 값만 쓴다.", key="basic-plan", builtin=True),
    Template("검토보고서", "검토보고서", "자료·법령을 검토한 결과를 보고하는 문서", _sections(
        ("1. 검토 개요", "검토 목적·대상·범위."),
        ("2. 검토 대상 및 사실관계", "검토한 문서와 확인된 사실."),
        ("3. 관련 근거", "자료에 적힌 관련 규정·지침·선례."),
        ("4. 주요 검토 내용", "쟁점별 검토 내용."),
        ("5. 문제점 및 유의사항", "자료끼리 엇갈리는 점, 확인되지 않은 점."),
        ("6. 조치 의견", "후속 조치. 자료에 근거가 없으면 비운다.")),
        "확인된 사실과 의견을 구분해 쓴다. 자료가 엇갈리면 양쪽을 모두 적는다.",
        key="review-report", builtin=True),
    Template("업무 브리핑", "브리핑", "한 업무의 핵심을 한 장으로 정리한 문서", _sections(
        ("1. 업무 개요", "어떤 업무인지 한두 문장으로."),
        ("2. 기한과 일정", "제출기한·시행일·반복 시기."),
        ("3. 처리 절차", "누가 무엇을 어떤 순서로 하는지."),
        ("4. 제출처와 서식", "어디에 무엇을 내는지."),
        ("5. 확인이 필요한 사항", "자료만으로 알 수 없거나 엇갈리는 것.")),
        "후임자가 바로 일을 시작할 수 있게 짧게 쓴다.", key="briefing", builtin=True),
    Template("회의록", "회의록", "회의의 논의와 결정을 기록하는 문서", _sections(
        ("1. 회의 개요", "일시·장소·참석자·안건."),
        ("2. 주요 논의 내용", "안건별 논의."),
        ("3. 결정 사항", "합의·결정된 것만."),
        ("4. 후속 조치", "조치 사항·담당·기한.")),
        "발언을 지어내지 않는다. 자료에 적힌 내용만 옮긴다.", key="minutes", builtin=True),
    Template("보도자료", "보도자료", "정책·사업을 알리는 보도자료", _sections(
        ("1. 보도 요지", "핵심 내용을 두세 문장으로."),
        ("2. 추진 배경", "왜 추진하는지."),
        ("3. 주요 내용", "무엇이 달라지는지, 누가 혜택을 받는지."),
        ("4. 향후 일정", "앞으로의 일정."),
        ("5. 문의처", "담당 부서. 자료에 없으면 비운다.")),
        "행정 용어를 쉬운 말로 풀어 서술식으로 쓴다.", key="press-release", builtin=True),
]


def _row_to_template(row) -> Template:
    sections = [Section(**item) for item in json.loads(row["sections"] or "[]")]
    fmt = json.loads(row["format"]) if row["format"] else copy.deepcopy(DEFAULT_FORMAT)
    return Template(row["title"], row["doc_type"], row["description"], sections, row["guidance"],
                    fmt, row["key"] or "", bool(row["builtin"]), row["id"])


def ensure_builtin(db) -> None:
    """내장 서식을 프로젝트에 심는다. 이미 있는 것은 건드리지 않는다.

    화면이 서식 목록을 자주 읽으므로 다 심겨 있으면 쓰지 않는다 — 읽을 때마다
    쓰기를 걸면 뒤에서 도는 분석과 DB 잠금을 다툰다.
    """
    seeded = db.con.execute("SELECT COUNT(*) FROM doc_templates WHERE builtin=1").fetchone()[0]
    if seeded >= len(BUILTIN):
        return
    for template in BUILTIN:
        db.con.execute(
            "INSERT OR IGNORE INTO doc_templates(key,title,doc_type,description,sections,guidance,format,builtin) "
            "VALUES (?,?,?,?,?,?,?,1)",
            (template.key, template.title, template.doc_type, template.description,
             json.dumps([{"title": s.title, "instruction": s.instruction} for s in template.sections],
                        ensure_ascii=False),
             template.guidance, json.dumps(template.format, ensure_ascii=False)))


def all_templates(db) -> list[Template]:
    ensure_builtin(db)
    return [_row_to_template(row) for row in db.con.execute(
        "SELECT * FROM doc_templates ORDER BY builtin DESC, id")]


def get(db, template_id: int) -> Template:
    ensure_builtin(db)
    row = db.con.execute("SELECT * FROM doc_templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise ValueError("서식을 선택하세요.")
    return _row_to_template(row)


def save(db, template: Template) -> int:
    """사용자 서식을 저장한다. 내장 서식을 고치면 사본으로 저장한다."""
    title = template.title.strip()
    sections = [s for s in template.sections if s.title.strip()]
    if not title or not sections:
        raise ValueError("서식 이름과 항목을 하나 이상 입력하세요.")
    values = (title, template.doc_type.strip() or "기타", template.description, json.dumps(
        [{"title": s.title.strip(), "instruction": s.instruction.strip()} for s in sections], ensure_ascii=False),
        template.guidance, json.dumps(template.format, ensure_ascii=False))
    if template.id is not None and not template.builtin:
        db.con.execute("UPDATE doc_templates SET title=?,doc_type=?,description=?,sections=?,guidance=?,"
                       "format=?,updated_at=datetime('now') WHERE id=? AND builtin=0", (*values, template.id))
        db.audit("studio.template.save", str(template.id))
        return template.id
    key = db.con.execute("INSERT INTO doc_templates(title,doc_type,description,sections,guidance,format,builtin) "
                         "VALUES (?,?,?,?,?,?,0)", values).lastrowid
    db.audit("studio.template.save", str(key))
    return key


def delete(db, template_id: int) -> None:
    row = db.con.execute("SELECT builtin FROM doc_templates WHERE id=?", (template_id,)).fetchone()
    if row is None or row["builtin"]:
        raise ValueError("내장 서식은 지울 수 없습니다.")
    db.con.execute("DELETE FROM doc_templates WHERE id=?", (template_id,))
    db.audit("studio.template.delete", str(template_id))
