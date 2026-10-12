"""과부하·시연용 합성 자료 생성기.
    python -m app.tools.make_stress_fixtures [출력폴더] [--profile hard]
`make_fixtures`의 표본(77건·2MB)은 파서와 시점 추정이 '되는지'를 본다. 이 생성기는
'버티는지'를 본다 — 실제 인계 폴더처럼 수가 많고, 크고, 어지럽고, 일부는 깨져 있다.
일부러 재현하는 것
  - 업무 24종 × 여러 해. 이름이 비슷한 업무(국정감사·종합감사·행정사무감사)와
    한 업무의 여러 이름(근무성적평정·근평)이 섞인다 (업무 파악 검증)
  - 매월·분기·반기·매년·비정기 반복과, 진행 중인 올해(9월까지) (일정 파악 검증)
  - 주간·일일 보고처럼 작고 많은 파일, 수백 쪽·수천 행의 큰 파일
  - 그림이 대부분인 문서와 스캔본 — 용량은 크지만 글은 적다
  - 판본 난립, 판본 사이에 엇갈리는 금액, 완전 중복, 폴더째 복사해 수정일이 틀린 자료
  - 손상·암호·빈 파일·구형 형식·크기 상한 초과·CP949/UTF-16·자소 분리(NFD) 파일명
같은 seed면 같은 경로와 같은 본문이 나온다. 결과와 함께 정답 원장(manifest)을
출력폴더 **밖**에 쓴다 — 안에 두면 그 파일도 전임자 자료로 훑는다.
⚠ .hwp(HWP 5.0)는 여기서도 합성하지 못한다. 실제 문서로 따로 검증해야 한다.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from ..ingest.parsers import supported_extensions
from ..ingest.parsers.base import MAX_BYTES, MAX_CELLS_PER_SHEET, MAX_CHARS
from .console import force_utf8

DEFAULT_OUT = Path("tests/fixtures/전임자업무_샘플/공유폴더_백업")
DEFAULT_MANIFEST = Path("tests/fixtures/stress_manifest.json")
DEFAULT_SEED = 20261010
# 진행 중인 해. 이 해는 THIS_MONTH까지만 자료가 있다.
THIS_YEAR, THIS_MONTH = 2026, 9
# 출력폴더 아래 상대 경로의 길이 상한. Windows의 260자 제한 안에 머문다.
MAX_REL_PATH = 150


@dataclass(frozen=True)
class Profile:
    years: tuple[int, ...]
    monthly_years: int        # 매월 반복 업무를 만들 최근 연수
    weekly_step: int          # 주간보고를 몇 주마다 하나씩 만들지 (0이면 안 만든다)
    weekly_years: int
    daily_days: int           # 여름철 일일 상황보고 일수 (해마다)
    daily_years: int
    medium: int               # 편람·결과보고서·회의록 등 긴 문서 수
    medium_chars: int
    scale: float              # 큰 파일의 행·쪽 수 배율
    photos: int               # 현장 사진 등 문서가 아닌 파일 수
    image_px: tuple[int, int]
    scan_pages: int
    oversize: bool            # 크기 상한(200MB)을 넘는 파일
    full_text_docs: int       # 추출 한도(MAX_CHARS)에 닿는 문서 수


PROFILES: dict[str, Profile] = {
    # 시험용 — 몇 초 안에 모든 종류를 하나씩 만든다.
    "smoke": Profile((2025, 2026), 1, 13, 1, 3, 1, 2, 3_000, 0.03, 3, (200, 280), 2, False, 0),
    # 시연용 — 분석이 한 시간 안쪽에 끝난다(CPU 임베딩 기준).
    "demo": Profile(tuple(range(2022, 2027)), 2, 2, 1, 20, 1, 8, 12_000, 0.3, 12, (800, 1130), 8, False, 0),
    # 기본 — 1천 건 남짓, 수백 MB.
    "hard": Profile(tuple(range(2019, 2027)), 4, 1, 2, 62, 2, 30, 20_000, 1.0, 40, (1240, 1754), 30, True, 0),
    # 한계 시험 — 수천 건. 추출 한도에 닿는 문서가 들어 있어 임베딩이 매우 오래 걸린다.
    "extreme": Profile(tuple(range(2012, 2027)), 15, 1, 6, 62, 6, 120, 40_000, 3.0, 200, (1654, 2339), 60, True, 3),
}


# ── 업무 ────────────────────────────────────────────────────────────
MONTHLY = tuple(range(1, 13))
QUARTERLY = (3, 6, 9, 12)


@dataclass(frozen=True)
class Task:
    name: str
    aliases: tuple[str, ...]            # 파일명·폴더명에 번갈아 쓰이는 이름
    recipient: str                      # 제출처
    steps: tuple[tuple[int, str], ...]  # (월, 단계). 월 0은 해마다 달라지는 비정기
    items: tuple[str, ...]              # 이 업무에만 나오는 낱말 — 업무끼리 섞이지 않게
    exts: tuple[str, ...] = ("hwpx", "hwpx", "docx", "pdf", "odt", "xlsx", "txt")
    only_years: tuple[int, ...] = ()
    every: int = 1                      # 2면 홀수 해에만
    monthly: bool = False


def _each(months: tuple[int, ...], step: str) -> tuple[tuple[int, str], ...]:
    return tuple((month, step) for month in months)


TASKS: tuple[Task, ...] = (
    Task("국정감사대응", ("국정감사", "국감"), "환경부 감사관실",
         ((9, "요구자료 접수"), (9, "요구자료 취합"), (10, "제출자료 검토"), (10, "수감 결과보고")),
         ("유수율 현황", "노후관 교체 실적", "수돗물 음용률", "국고보조금 집행", "위원 요구목록")),
    Task("결산", ("결산", "세입세출결산"), "회계과 결산팀",
         ((1, "결산 작성지침 접수"), (2, "결산서 작성"), (4, "결산검사 자료 제출"), (6, "결산 승인 보고")),
         ("세입결산액", "이월액", "불용액", "채권 현재액", "결산검사위원 지적"), ("xlsx", "xlsx", "hwpx", "pdf")),
    Task("주요업무보고", ("주요업무보고", "업무보고"), "기획예산과",
         ((1, "상반기 주요업무보고"), (7, "하반기 주요업무보고")),
         ("역점시책", "공약사항 이행", "신규사업", "현안사항", "건의사항"), ("pptx", "hwpx", "pdf")),
    Task("정보공개청구", ("정보공개청구", "정보공개"), "민원봉사과 정보공개담당",
         ((0, "청구서 접수"), (0, "공개 결정 통지서 송부")),
         ("청구인 통지", "비공개 사유", "부분공개", "이의신청", "수수료 감면")),
    Task("민원처리", ("민원처리", "민원"), "민원봉사과",
         _each(MONTHLY, "민원처리 현황 보고"),
         ("누수 신고", "수질 문의", "요금 이의", "계량기 교체 요청", "단수 안내"), ("xlsx", "xlsx", "hwpx"),
         monthly=True),
    Task("물품관리", ("물품관리", "재물조사"), "회계과 재산관리팀",
         ((6, "정기 재물조사 계획"), (7, "재물조사 결과보고"), (12, "불용물품 처분 계획")),
         ("내용연수 경과 물품", "불용 결정", "물품출납부", "재물조사표", "매각 수입")),
    Task("보안점검", ("보안점검", "사이버보안진단"), "정보통신과 보안담당",
         _each(MONTHLY, "사이버보안 진단의날 점검결과 제출"),
         ("백신 최신화", "화면보호기 설정", "비인가 USB", "취약 계정", "보안서약서"), ("xlsx", "hwpx", "txt"),
         monthly=True),
    Task("교육훈련", ("교육훈련", "직원교육"), "총무과 교육담당",
         ((2, "연간 교육훈련 계획"), (8, "상반기 교육실적 제출"), (12, "교육훈련 결과보고")),
         ("상시학습 시간", "직무전문교육", "사이버교육 이수", "신규자 멘토링", "교육 여비")),
    Task("성과평가", ("성과평가", "BSC"), "기획예산과 성과관리팀",
         ((3, "성과지표 설정 제출"), (7, "상반기 실적 점검"), (12, "성과평가 결과 제출")),
         ("핵심성과지표", "목표 달성도", "가중치", "실적 증빙", "이의신청 기간")),
    Task("청렴시책", ("청렴시책", "반부패청렴"), "감사실 청렴담당",
         ((3, "청렴시책 추진계획"), (9, "청렴도 자체점검 결과"), (11, "부패방지 시책평가 자료 제출")),
         ("청렴 서약", "청탁금지법 교육", "공사 감독 청렴 해피콜", "이해충돌 신고", "청렴 마일리지")),
    Task("시설물안전점검", ("시설물안전점검", "안전점검"), "안전총괄과",
         ((3, "해빙기 안전점검 계획"), (4, "해빙기 점검 결과보고"),
          (10, "동절기 대비 점검 계획"), (11, "동절기 점검 결과 송부")),
         ("배수지 균열", "가압장 전기설비", "정수장 약품탱크", "옹벽 배부름", "보수·보강 조치")),
    Task("체납정리", ("수도요금체납정리", "체납정리"), "세정과 체납관리팀",
         _each(QUARTERLY, "체납액 정리 실적 보고"),
         ("정수처분 예고", "분할납부 승인", "결손처분", "고액 체납자", "압류 해제")),
    Task("공유재산", ("공유재산", "공유재산실태조사"), "회계과 재산관리팀",
         ((5, "공유재산 실태조사 계획"), (7, "실태조사 결과 제출")),
         ("무단 점유", "대부료 부과", "용도폐지", "재산대장 불일치", "변상금")),
    Task("기록물관리", ("기록물관리", "기록물정리"), "총무과 기록연구사",
         ((2, "기록물 정리 계획"), (3, "생산현황 통보"), (9, "기록물 이관 목록 제출")),
         ("보존기간 책정", "비전자기록물 편철", "생산현황 통보서", "이관 대상 권수", "폐기 심의")),
    Task("재난안전훈련", ("재난안전훈련", "안전한국훈련"), "안전총괄과 재난대응팀",
         ((4, "훈련 기본계획 수립"), (5, "훈련 시나리오 검토"), (6, "훈련 결과보고")),
         ("대규모 단수 가상상황", "비상급수 차량", "상황판단회의", "유관기관 협조", "훈련 평가위원")),
    Task("급수공사", ("급수공사", "급수공사계약"), "회계과 계약팀",
         ((0, "급수공사 계약 체결 요청"), (0, "준공검사 결과보고")),
         ("인입관 구경", "도로 굴착 복구", "시공업체 하자보증", "준공계", "설계변경 사유"),
         ("pdf", "hwpx", "xlsx", "docx")),
    Task("언론보도대응", ("언론보도대응", "보도자료"), "홍보담당관",
         ((0, "보도 해명자료 제출"), (0, "보도 동향 보고")),
         ("적수 보도", "해명 요지", "기자 문의", "정정보도 요청", "보도 스크랩")),
    Task("위원회운영", ("수도요금심의위원회", "위원회운영"), "기획예산과 위원회담당",
         ((2, "위원회 개최 계획"), (5, "회의 결과보고"), (8, "위원회 개최 계획"), (11, "회의 결과보고")),
         ("위촉직 위원", "심의 안건", "참석 수당", "서면 심의", "회의록 공개")),
    Task("근무성적평정", ("근무성적평정", "근평"), "총무과 인사팀",
         ((1, "하반기 근무성적평정 자료 제출"), (7, "상반기 근무성적평정 자료 제출")),
         ("평정 단위", "성과계획서", "평정자 의견", "서열명부", "가점 증빙")),
    Task("초과근무", ("초과근무", "시간외근무"), "총무과 복무담당",
         _each(MONTHLY, "초과근무 실적 취합"),
         ("시간외근무수당", "사전 명령", "월 상한 시간", "휴일 근무", "복무 점검"), ("xlsx", "xlsx", "csv"),
         monthly=True),
    Task("조례개정", ("수도급수조례개정", "조례개정"), "법무담당관",
         ((3, "조례 개정 계획"), (4, "입법예고 의견 접수"), (6, "조례규칙심의회 안건 제출"), (9, "공포 결과 송부")),
         ("신구조문 대비표", "입법예고 기간", "규제 심사", "비용추계서", "부칙 경과조치"),
         only_years=(2023,)),
    Task("공모사업", ("스마트관망공모사업", "공모사업"), "환경부 물이용기획과",
         ((2, "공모 신청서 제출"), (5, "선정 결과 통보 접수"), (11, "사업 정산 보고")),
         ("스마트 미터", "원격 누수감지", "국비 매칭 비율", "사업계획서 발표평가", "정산 잔액 반납"),
         only_years=(2022, 2023, 2024)),
    Task("종합감사", ("종합감사", "자체감사"), "감사실 감사팀",
         ((5, "감사자료 요구 접수"), (6, "감사자료 제출"), (8, "처분요구 조치결과 제출")),
         ("시정 요구", "주의 처분", "회수 금액", "감사 대상기간", "현지 조치"), every=2),
    Task("차량관리", ("공용차량관리", "차량관리"), "회계과 차량담당",
         _each(QUARTERLY, "공용차량 운행 실적 보고"),
         ("운행일지", "유류비", "정기검사", "차량 교체 기준", "전기차 충전")),
)


_TEAMS = ("운영팀", "시설팀", "요금팀", "수질팀", "관리팀", "급수팀")
_SURNAMES = "김이박최정강조윤장임한오서신권황"
_LAWS = ("수도법", "지방회계법", "공유재산 및 물품 관리법", "공공기록물 관리에 관한 법률",
         "지방자치법", "공공기관의 정보공개에 관한 법률", "민원 처리에 관한 법률", "지방공무원법")


_DONGS = ("중앙동", "신흥동", "역전동", "남산동", "북부동", "강변동", "호수동", "산성동")
_VERSIONS = ("_수정", "_최종", "_최종2", "_진짜최종", "_부장수정", "_송부", "(1)", "_v2", " - 복사본")
_PROCEDURES = (
    ("요구목록 접수", "부서별 배부", "자료 취합", "부서장 결재", "제출"),
    ("계획 수립", "관련 부서 협조 요청", "자료 작성", "팀장 검토", "과장 결재", "송부"),
    ("공문 접수", "담당자 지정", "현황 조사", "결과 정리", "보고"),
)


_SECTIONS = ("추진 배경", "관련 근거", "그간의 경과", "추진 방향", "세부 추진계획", "소요예산",
             "기관별 역할", "문제점 및 대책", "전년도 지적사항", "향후 일정", "행정사항", "담당자 유의사항")


# 긴 문서를 채우는 문장. 같은 문장이 되풀이되면 검색이 쉬워지므로 빈칸을 매번 바꾼다.
_FILLER = (
    "{team}에서는 {item} 관련 자료를 {m}월 {d}일까지 취합하여 {recv}에 제출하였다.",
    "{y}년 {item} 실적은 총 {n}건으로 전년 대비 {pct}% {trend}하였다.",
    "{item}에 대해서는 담당자가 현장 확인을 마친 뒤 결과를 {team}장에게 보고한다.",
    "지난 점검에서 {item} 관련 지적 {k}건 가운데 {j}건은 조치를 마쳤고 나머지는 {m2}월까지 끝낼 예정이다.",
    "{item}에 드는 비용은 {amt}천원으로 보았으며, 모자라는 금액은 추경에 반영을 요청하기로 하였다.",
    "자료를 쓸 때 {item}의 기준일은 전월 말일로 하고, 금액은 천원, 실적은 건으로 통일한다.",
    "{recv} 담당자와 미리 협의한 결과 {item} 서식은 전년도와 같게 쓰기로 하였다.",
    "{item} 누락이 되풀이되어 {y}년부터는 제출 전에 {team} 팀장의 교차 검토를 거친다.",
    "관련 근거는 「{law}」 제{a}조와 같은 법 시행령 제{b}조이다.",
    "{item} 담당이 휴가나 교육으로 자리를 비우면 업무대행자가 {recv} 회신을 맡는다.",
    "시스템 입력은 매월 {d}일 18시에 닫히며, 그 뒤의 수정은 {recv}의 승인이 있어야 한다.",
    "회의에서는 {item} 처리가 늦어진 까닭과 재발을 막을 방법을 주로 논의하였다.",
    "{dong} 일대의 {item} 건은 {m}월 중 현장 조사를 하고 그 결과를 다음 보고에 싣는다.",
    "{item} 통계는 {team}과 {team2}의 집계가 {k}건 달라 원자료를 다시 맞춰 보았다.",
    "{y}년 {m}월 기준 {item} 진행률은 {pct}%이며 연말까지 목표를 채울 것으로 본다.",
    "{recv}에서 {item} 보완을 요구하면 접수한 날부터 {j}일 안에 회신한다.",
)


# ── 문서 모형 ───────────────────────────────────────────────────────
@dataclass
class Doc:
    """형식에 매이지 않는 문서. blocks는 ("h1"|"h2"|"p", 글) 또는 ("table", 행들, 단위)."""

    title: str
    blocks: list[tuple] = field(default_factory=list)
    when: datetime = datetime(2025, 1, 2, 9, 0)
    author: str = "홍길동"
    notes: str = ""
    images: list[bytes] = field(default_factory=list)

    def lines(self, cell_sep: str = "\t") -> list[str]:
        out: list[str] = []
        for block in self.blocks:
            if block[0] == "table":
                if block[2]:
                    out.append(f"(단위: {block[2]})")
                out.extend(cell_sep.join(row) for row in block[1])
            else:
                out.append(block[1])
        return out
    @property
    def chars(self) -> int:
        return sum(len(line) for line in self.lines())


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _wrap(text: str, width: int = 42) -> list[str]:
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]


# ── 형식별 쓰기 ─────────────────────────────────────────────────────
def _write_text(path: Path, doc: Doc, encoding: str = "utf-8") -> None:
    ext = path.suffix.lower()
    parts: list[str] = []
    for block in doc.blocks:
        if block[0] == "table":
            sep = "," if ext == ".csv" else ("|" if ext == ".md" else "\t")
            head = [f"(단위: {block[2]})"] if block[2] else []
            parts.append("\n".join(head + [sep.join(row) for row in block[1]]))
        elif ext == ".md" and block[0] in ("h1", "h2"):
            parts.append(("# " if block[0] == "h1" else "## ") + block[1])
        else:
            parts.append(block[1])
    path.write_bytes("\n\n".join(parts).encode(encoding, "replace"))


def _write_docx(path: Path, doc: Doc) -> None:
    import docx
    from docx.shared import Inches

    out = docx.Document()
    out.core_properties.author = doc.author
    out.core_properties.title = doc.title
    out.core_properties.created = doc.when
    out.core_properties.modified = doc.when
    for block in doc.blocks:
        if block[0] in ("h1", "h2"):
            out.add_heading(block[1], level=1 if block[0] == "h1" else 2)
        elif block[0] == "p":
            out.add_paragraph(block[1])
        else:
            rows = block[1]
            if block[2]:
                out.add_paragraph(f"(단위: {block[2]})")
            table = out.add_table(rows=len(rows), cols=len(rows[0]))
            for r, row in enumerate(rows):
                cells = table.rows[r].cells
                for c, value in enumerate(row):
                    cells[c].text = value
    for image in doc.images:
        out.add_picture(io.BytesIO(image), width=Inches(6))
        out.add_paragraph("사진 설명 없음")
    out.save(str(path))


def _write_xlsx(path: Path, doc: Doc) -> None:
    import openpyxl

    wb = openpyxl.Workbook(write_only=True)
    body = wb.create_sheet("본문")
    number = 0
    for block in doc.blocks:
        if block[0] == "table":
            number += 1
            sheet = wb.create_sheet(f"표{number}")
            for row in block[1]:
                sheet.append([int(v) if v.isdigit() else v for v in row])
        else:
            body.append([block[1]])
    wb.properties.creator = doc.author
    wb.properties.title = doc.title
    wb.properties.created = doc.when
    wb.properties.modified = doc.when
    wb.save(str(path))


def _write_pptx(path: Path, doc: Doc) -> None:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    deck = Presentation()
    deck.core_properties.author = doc.author
    deck.core_properties.title = doc.title
    deck.core_properties.created = doc.when
    deck.core_properties.modified = doc.when
    state = {"frame": None, "used": 0}

    def new_slide(title: str):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = title
        state["frame"], state["used"] = None, 0
        return slide

    slide = new_slide(doc.title)
    if doc.notes:
        slide.notes_slide.notes_text_frame.text = doc.notes
    heading = doc.title
    for block in doc.blocks:
        if block[0] in ("h1", "h2"):
            heading = block[1]
            slide = new_slide(heading)
        elif block[0] == "p":
            if state["used"] + len(block[1]) > 500:
                slide = new_slide(heading + " (계속)")
            if state["frame"] is None:
                box = slide.shapes.add_textbox(Inches(0.6), Inches(1.5), Inches(8.8), Inches(5))
                state["frame"] = box.text_frame
                state["frame"].word_wrap = True
                state["frame"].text = block[1]
            else:
                state["frame"].add_paragraph().text = block[1]
            for paragraph in state["frame"].paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(14)
            state["used"] += len(block[1])
        else:
            rows = block[1][:12]
            slide = new_slide(heading + " (표)")
            shape = slide.shapes.add_table(len(rows), len(rows[0]), Inches(0.5), Inches(1.5),
                                           Inches(9), Inches(0.4 * len(rows)))
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    shape.table.cell(r, c).text = value
    for image in doc.images:
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_picture(io.BytesIO(image), Inches(0.5), Inches(0.5), height=Inches(6.5))
    deck.save(str(path))


def _write_pdf(path: Path, doc: Doc, password: str = "") -> None:
    """한글은 PyMuPDF에 든 CJK 글꼴("korea")로 쓴다. 그림은 한 쪽에 하나씩 스캔본처럼 넣는다."""
    import pymupdf

    out = pymupdf.open()
    lines: list[str] = []
    for line in doc.lines(" | "):
        lines.extend(_wrap(line))
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()   # 빈 줄만 있는 마지막 쪽은 '글을 못 얻은 쪽'으로 세어진다
    per_page = 46
    for start in range(0, len(lines), per_page):
        page = out.new_page()
        page.insert_text((72, 80), "\n".join(lines[start:start + per_page]),
                         fontname="korea", fontsize=10)
    for image in doc.images:
        page = out.new_page()
        page.insert_image(page.rect, stream=image)
    if not out.page_count:
        out.new_page()
    stamp = doc.when.strftime("D:%Y%m%d%H%M%S+09'00'")
    out.set_metadata({"title": doc.title, "author": doc.author,
                      "creationDate": stamp, "modDate": stamp})
    if password:
        out.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 user_pw=password, owner_pw=password + "-owner")
    else:
        out.save(str(path), deflate=True)
    out.close()


_HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
_HS = "http://www.hancom.co.kr/hwpml/2011/section"


def _hwpx_paragraph(text: str) -> str:
    return f"<hp:p><hp:run><hp:t>{_esc(text)}</hp:t></hp:run></hp:p>"


def _hwpx_table(rows: list[list[str]], merge_first: int = 0) -> str:
    """merge_first가 있으면 첫 열을 그 행 수만큼 세로로 병합한다(머리 행 제외)."""
    out = [f'<hp:p><hp:run><hp:tbl rowCnt="{len(rows)}" colCnt="{len(rows[0])}">']
    for r, row in enumerate(rows):
        out.append("<hp:tr>")
        for c, value in enumerate(row):
            span = 1
            if merge_first and c == 0 and r > 0:
                if (r - 1) % merge_first:
                    continue   # 위 칸의 병합에 덮인 칸은 파일에 없다
                span = min(merge_first, len(rows) - r)
            head = ' header="1"' if r == 0 else ""
            out.append(
                f"<hp:tc{head}><hp:subList>{_hwpx_paragraph(value)}</hp:subList>"
                f'<hp:cellAddr colAddr="{c}" rowAddr="{r}"/>'
                f'<hp:cellSpan colSpan="1" rowSpan="{span}"/></hp:tc>')
        out.append("</hp:tr>")
    out.append("</hp:tbl></hp:run></hp:p>")
    return "".join(out)


def _write_hwpx(path: Path, doc: Doc, merge_first: int = 0, extra: dict[str, str] | None = None,
                sections: bool = True) -> None:
    parts: list[str] = []
    for block in doc.blocks:
        if block[0] == "table":
            if block[2]:
                parts.append(_hwpx_paragraph(f"(단위: {block[2]})"))
            parts.append(_hwpx_table(block[1], merge_first))
        else:
            parts.append(_hwpx_paragraph(block[1]))
    section = (f'<?xml version="1.0" encoding="UTF-8"?>'
               f'<hs:sec xmlns:hs="{_HS}" xmlns:hp="{_HP}">{"".join(parts)}</hs:sec>')
    package = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<opf:package xmlns:opf="http://www.idpf.org/2007/opf/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><opf:metadata>'
        f"<dc:title>{_esc(doc.title)}</dc:title><dc:creator>{_esc(doc.author)}</dc:creator>"
        f"<dc:date>{doc.when.isoformat()}</dc:date></opf:metadata></opf:package>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("version.xml", '<?xml version="1.0"?><hv:HCFVersion xmlns:hv="x"/>')
        zf.writestr("Contents/content.hpf", package)
        if sections:
            zf.writestr("Contents/section0.xml", section)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)


_ODF = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
}


def _write_odt(path: Path, doc: Doc) -> None:
    spaces = " ".join(f'xmlns:{name}="{uri}"' for name, uri in _ODF.items())
    parts: list[str] = []
    for block in doc.blocks:
        if block[0] in ("h1", "h2"):
            level = 1 if block[0] == "h1" else 2
            parts.append(f'<text:h text:outline-level="{level}">{_esc(block[1])}</text:h>')
        elif block[0] == "p":
            parts.append(f"<text:p>{_esc(block[1])}</text:p>")
        else:
            if block[2]:
                parts.append(f"<text:p>(단위: {_esc(block[2])})</text:p>")
            rows = "".join(
                "<table:table-row>" + "".join(
                    f"<table:table-cell><text:p>{_esc(v)}</text:p></table:table-cell>" for v in row)
                + "</table:table-row>" for row in block[1])
            parts.append(f"<table:table>{rows}</table:table>")
    content = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {spaces}>'
               f'<office:body><office:text>{"".join(parts)}</office:text></office:body>'
               "</office:document-content>")
    meta = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-meta {spaces}><office:meta>'
            f"<dc:title>{_esc(doc.title)}</dc:title>"
            f"<meta:initial-creator>{_esc(doc.author)}</meta:initial-creator>"
            f"<meta:creation-date>{doc.when.isoformat()}</meta:creation-date>"
            "</office:meta></office:document-meta>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", content)
        zf.writestr("meta.xml", meta)


def _write(path: Path, doc: Doc) -> None:
    ext = path.suffix.lower()
    if ext == ".docx":
        _write_docx(path, doc)
    elif ext in (".xlsx", ".xlsm"):
        _write_xlsx(path, doc)
    elif ext == ".pptx":
        _write_pptx(path, doc)
    elif ext == ".pdf":
        _write_pdf(path, doc)
    elif ext == ".hwpx":
        _write_hwpx(path, doc)
    elif ext == ".odt":
        _write_odt(path, doc)
    else:
        _write_text(path, doc)


def _jpeg(rng: random.Random, size: tuple[int, int], quality: int = 70) -> bytes:
    """잡음 그림. 압축되지 않아 용량을 채우고, 글자가 없어 스캔본 판정을 시험한다."""
    from PIL import Image

    width, height = size
    image = Image.frombytes("L", (width, height), rng.randbytes(width * height))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


# ── 생성 ────────────────────────────────────────────────────────────
class Generator:
    def __init__(self, out: Path, profile: Profile, seed: int = DEFAULT_SEED, force: bool = False):
        self.out = out
        self.profile = profile
        self.seed = seed
        self.force = force
        self.entries: list[dict] = []
        self.questions: list[dict] = []
        self.written = 0
        self.chars = 0
        self._used: set[str] = set()

    def rng(self, *key) -> random.Random:
        return random.Random(f"{self.seed}:" + ":".join(str(part) for part in key))

    # 기록과 쓰기 ----------------------------------------------------
    def place(self, folder: str, name: str, ext: str) -> str:
        """출력폴더 아래의 겹치지 않는 상대 경로. 길면 이름을 줄인다."""
        room = MAX_REL_PATH - len(folder) - len(ext) - 6
        name = name[:max(room, 12)].rstrip(" .")
        rel = f"{folder}/{name}.{ext}" if folder else f"{name}.{ext}"
        number = 1
        while rel.casefold() in self._used:
            number += 1
            rel = f"{folder}/{name} ({number}).{ext}"
        self._used.add(rel.casefold())
        return rel

    def emit(self, rel: str, kind: str, write, *, when: datetime | None = None,
             expect: list[str] | None = None, chars: int = 0, **info) -> str:
        """write(path)로 파일을 만들고 원장에 적는다. 이미 있으면 다시 만들지 않는다."""
        self._used.add(rel.casefold())
        path = self.out / rel
        if self.force or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            write(path)
            if when is not None:
                stamp = when.timestamp()
                os.utime(path, (stamp, stamp))
            self.written += 1
        supported = path.suffix.lower() in supported_extensions()
        self.chars += chars
        self.entries.append({
            "path": rel, "kind": kind, "bytes": path.stat().st_size, "document": supported,
            "expect": expect if expect is not None else (["ok"] if supported else None),
            "chars": chars, **info,
        })
        return rel

    def emit_doc(self, rel: str, kind: str, doc: Doc, *, mtime: datetime | None = None, **info) -> str:
        return self.emit(rel, kind, lambda path: _write(path, doc), when=mtime or doc.when,
                         chars=doc.chars, **info)

    # 본문 재료 ------------------------------------------------------
    def person(self, rng: random.Random) -> str:
        return f"{rng.choice(_SURNAMES)}○○ 주무관"

    def filler(self, rng: random.Random, task: Task, year: int) -> str:
        team, team2 = rng.sample(_TEAMS, 2)
        k = rng.randint(3, 19)
        return rng.choice(_FILLER).format(
            team=team, team2=team2, item=rng.choice(task.items), recv=task.recipient, y=year,
            m=rng.randint(1, 12), m2=rng.randint(1, 12), d=rng.randint(1, 28),
            n=rng.randint(12, 980), pct=rng.randint(2, 48), trend=rng.choice(("증가", "감소")),
            k=k, j=rng.randint(1, k), amt=f"{rng.randint(5, 900) * 100:,}", law=rng.choice(_LAWS),
            a=rng.randint(2, 80), b=rng.randint(2, 60), dong=rng.choice(_DONGS))

    def paragraph(self, rng: random.Random, task: Task, year: int, sentences: int = 0) -> str:
        return " ".join(self.filler(rng, task, year) for _ in range(sentences or rng.randint(3, 5)))

    def facts(self, task: Task, year: int) -> dict:
        """업무·연도에 고정된 사실. 같은 해의 문서들은 같은 값을 말한다."""
        rng = self.rng("facts", task.name, year)
        return {
            "amount": rng.randint(30, 4000) * 100,
            "contact": f"{rng.choice(_TEAMS)} {self.person(rng)} (내선 {rng.randint(2100, 2899)})",
            "procedure": rng.choice(_PROCEDURES),
        }

    def table(self, rng: random.Random, task: Task, rows: int = 0) -> tuple:
        head = ["항목", "계획", "실적", "비고"]
        body = [[item, str(rng.randint(5, 400)), str(rng.randint(5, 400)),
                 rng.choice(("완료", "진행 중", "보완 요청", "해당 없음", ""))]
                for item in (rng.choices(task.items, k=rows) if rows else task.items)]
        return ("table", [head, *body], "건")

    def step_doc(self, task: Task, alias: str, year: int, month: int, day: int, step: str,
                 amount: int | None = None, reuse_year: int | None = None) -> tuple[Doc, dict]:
        """단계 문서 하나. reuse_year는 전년도 문서를 고쳐 쓰다 본문 연도를 못 고친 경우다."""
        rng = self.rng("step", task.name, year, month, step)
        facts = self.facts(task, year)
        amount = facts["amount"] if amount is None else amount
        when = datetime(year, month, day, rng.randint(9, 17), rng.randint(0, 59))
        due = when + timedelta(days=rng.randint(5, 16))
        shown = reuse_year or year
        style = rng.randrange(4)
        date_text = (f"{shown}. {month}. {day}.", f"{shown}-{month:02d}-{day:02d}",
                     f"{shown}년 {month}월 {day}일", f"{shown}.{month:02d}.{day:02d}.")[style]
        due_text = f"{due.year}. {due.month}. {due.day}."
        blocks: list[tuple] = [
            ("h1", f"{shown}년 {alias} {step}"),
            ("p", f"문서번호: 상수도과-{rng.randint(100, 9999)}"),
            ("p", f"작성일: {date_text}"),
            ("p", f"제출처: {task.recipient}"),
            ("p", f"제출기한: {due_text}"),
            ("p", f"담당자: {facts['contact']}"),
        ]
        if rng.random() < 0.6:
            blocks.append(("p", f"예산액: {amount:,}천원"))
        blocks.append(("p", "처리 절차: " + " → ".join(
            f"{'①②③④⑤⑥'[i]} {name}" for i, name in enumerate(facts["procedure"]))))
        for _ in range(rng.randint(1, 3)):
            blocks.append(("p", self.paragraph(rng, task, year)))
        blocks.append(self.table(rng, task))
        blocks.append(("p", f"전년도({year - 1}년)에는 같은 단계를 {month}월에 처리하였다. 붙임: 관련 근거자료 1부."))
        doc = Doc(f"{year}년 {alias} {step}", blocks, when, self.person(rng).split()[0],
                  notes=f"{rng.choice(task.items)} 수치는 발표 전에 다시 확인할 것.")
        return doc, {"due": due_text, "amount": amount, "recipient": task.recipient,
                     "contact": facts["contact"]}

    # 1. 업무 단계 문서 ----------------------------------------------
    def task_documents(self) -> None:
        years = self.profile.years
        for task in TASKS:
            task_years = [y for y in years if (not task.only_years or y in task.only_years)
                          and (task.every == 1 or y % task.every == 1)]
            if task.monthly:
                task_years = task_years[-self.profile.monthly_years:]
            if not task_years and task.only_years:
                task_years = [years[-1]]   # 작은 프로필에서도 업무가 통째로 빠지지 않게 한다
            for year in task_years:
                for index, (month, step) in enumerate(task.steps):
                    rng = self.rng("plan", task.name, year, index)
                    if month == 0:
                        month = rng.randint(1, 12)
                        if rng.random() < 0.25:
                            continue           # 비정기 업무는 없는 해도 있다
                    if year == THIS_YEAR and month > THIS_MONTH:
                        continue
                    self._step_files(task, year, month, step, rng)

    def _step_files(self, task: Task, year: int, month: int, step: str, rng: random.Random) -> None:
        alias = rng.choice(task.aliases)
        day = rng.randint(2, 27)
        ext = rng.choice(task.exts)

        # 전년도 파일을 복사해 쓰다 본문의 연도를 못 고친 문서. 파일명·폴더는 올해다.
        reuse = year - 1 if rng.random() < 0.03 else None
        doc, facts = self.step_doc(task, alias, year, month, day, step, reuse_year=reuse)
        compact = step.replace(" ", "")
        name = (
            f"{year}_{alias}_{compact}",
            f"{alias} {step} ({year % 100}.{month:02d}.{day:02d})",
            f"{year}{month:02d}{day:02d}_{compact}",
            f"[{alias}] {step}_{year}년",
            f"{year}년 {month}월 {alias} {step}",
            f"붙임{rng.randint(1, 4)}. {step}",
            compact,
        )[rng.choices(range(7), weights=(30, 16, 14, 12, 14, 8, 6))[0]]
        folder, mtime = self._folder(task, alias, year, month, doc.when, rng)
        info = {"task": task.name, "year": year, "month": month, "step": step}
        rel = self.emit_doc(self.place(folder, name, ext), "step", doc, mtime=mtime,
                            facts=facts, reused_body_year=reuse, **info)
        if reuse is None and rng.random() < 0.12:
            self.questions.append({
                "question": f"{year}년 {task.name} '{step}'의 제출기한은 언제이고 어디에 내나요?",
                "answer": f"{facts['due']} / {facts['recipient']}", "source": rel})
        # 판본 난립. 일부 판본은 금액이 달라 자료가 엇갈린다.
        if rng.random() < 0.16:
            for suffix in rng.sample(_VERSIONS, rng.randint(1, 3)):
                changed = facts["amount"] + rng.choice((0, 0, 100, -200, 500)) * 10
                other, _facts = self.step_doc(task, alias, year, month, day, step, amount=changed)
                other.when = doc.when + timedelta(days=rng.randint(1, 9))
                self.emit_doc(self.place(folder, name + suffix, ext), "version", other,
                              conflict=changed != facts["amount"], **info)
        # 완전 중복 — 같은 본문을 다른 폴더에 한 번 더 둔다.
        if rng.random() < 0.06:
            other_folder = rng.choice(("99_기타", "새 폴더", "새 폴더 (2)", "바탕화면 정리"))
            self.emit_doc(self.place(other_folder, name, ext), "duplicate", doc, duplicate_of=rel, **info)

    def _folder(self, task: Task, alias: str, year: int, month: int, when: datetime,
                rng: random.Random) -> tuple[str, datetime]:
        """어지러운 폴더 구조. 폴더째 복사한 자료는 수정일이 복사한 날로 바뀐다."""
        if year <= 2022 and rng.random() < 0.2:
            return f"USB백업_20230314/{task.name}", datetime(2023, 3, 14, 17, rng.randint(0, 59))
        choice = rng.choices(range(8), weights=(34, 12, 16, 10, 8, 6, 6, 8))[0]
        folder = (
            f"01_업무별/{task.name}/{year}",
            f"01_업무별/{task.name}/{year}년 {alias}",
            f"02_연도별/{year}/{alias}",
            f"{year}년도 자료/{month:02d}월",
            "99_기타",
            rng.choice(("새 폴더", "새 폴더 (2)")),
            f"바탕화면 정리/{alias}",
            f"전임자 인계/★중요/{task.name}",
        )[choice]
        return folder, when + timedelta(days=rng.randint(0, 3))

    # 2. 작고 많은 보고 ----------------------------------------------
    def weekly(self) -> None:
        if not self.profile.weekly_step:
            return
        task = Task("주간업무보고", ("주간업무보고",), "기획예산과", (), (
            "금주 추진실적", "차주 추진계획", "협조 요청", "부서장 지시사항", "현안 메모"))
        for year in self.profile.years[-self.profile.weekly_years:]:
            for week in range(1, 53, self.profile.weekly_step):
                when = datetime(year, 1, 1, 16, 30) + timedelta(weeks=week - 1, days=3)
                if year == THIS_YEAR and when.month > THIS_MONTH:
                    break
                rng = self.rng("weekly", year, week)
                blocks = [("h1", f"주간업무보고 ({year}년 {week}주차)"),
                          ("p", f"작성일: {when.year}. {when.month}. {when.day}."),
                          ("p", "제출처: 기획예산과"),
                          ("h2", "금주 추진실적"), ("p", self.paragraph(rng, rng.choice(TASKS), year, 2)),
                          ("h2", "차주 추진계획"), ("p", self.paragraph(rng, rng.choice(TASKS), year, 2))]
                doc = Doc(f"주간업무보고 {year}년 {week}주차", blocks, when)
                name = rng.choice((f"주간업무보고({year}년 {week}주차)",
                                   f"주간업무_{when:%y%m%d}", f"{when:%Y%m%d} 주간보고"))
                self.emit_doc(self.place(f"주간업무보고/{year}", name, rng.choice(("hwpx", "hwpx", "xlsx", "txt"))),
                              "weekly", doc, task=task.name, year=year, month=when.month)

    def daily(self) -> None:
        for year in self.profile.years[-self.profile.daily_years:]:
            if year == THIS_YEAR:
                continue
            for offset in range(self.profile.daily_days):
                when = datetime(year, 7, 1, 8, 40) + timedelta(days=offset)
                rng = self.rng("daily", year, offset)
                text = (f"{when.month}월 {when.day}일 풍수해 대비 상황보고\n"
                        f"보고일자: {when.year}. {when.month}. {when.day}.\n"
                        f"비상근무 {rng.randint(2, 14)}명 / 누수 신고 {rng.randint(0, 9)}건 / "
                        f"단수 {rng.randint(0, 3)}건 / 배수지 수위 {rng.randint(55, 97)}%\n"
                        f"특이사항: {rng.choice(_DONGS)} {rng.choice(('관로 누수 복구 중', '이상 없음', '가압장 정전 후 복구', '탁수 민원 접수'))}")
                # 메모장에서 저장한 옛 파일은 CP949가 섞여 있다.
                encoding = "cp949" if offset % 3 == 0 else "utf-8"
                self.emit(self.place(f"풍수해 상황보고/{year}", f"상황보고_{when:%m%d}", "txt" if offset % 5 else "log"),
                          "daily", lambda path, t=text, e=encoding: path.write_bytes(t.encode(e)),
                          when=when, chars=len(text), task="풍수해상황보고", year=year, month=when.month)

    # 3. 긴 문서 -----------------------------------------------------
    def long_doc(self, rng: random.Random, task: Task, year: int, title: str, chars: int,
                 when: datetime, table_every: int = 4) -> Doc:
        facts = self.facts(task, year)
        blocks: list[tuple] = [
            ("h1", title), ("p", f"작성일: {when.year}. {when.month}. {when.day}."),
            ("p", f"담당자: {facts['contact']}"), ("p", f"제출처: {task.recipient}")]
        doc = Doc(title, blocks, when)
        section = 0
        while doc.chars < chars:
            section += 1
            blocks.append(("h2", f"{section}. {rng.choice(_SECTIONS)} — {rng.choice(task.items)}"))
            for _ in range(rng.randint(3, 6)):
                blocks.append(("p", self.paragraph(rng, task, year, rng.randint(4, 8))))
            if section % table_every == 0:
                blocks.append(self.table(rng, task, rng.randint(6, 30)))
        return doc

    def medium(self) -> None:
        kinds = (("업무편람", ("hwpx", "docx", "pdf")), ("결과보고서", ("hwpx", "docx", "pdf", "odt")),
                 ("대책회의 회의록", ("txt", "md", "docx")), ("용역 과업지시서", ("pdf", "hwpx")),
                 ("추진계획 보고", ("pptx", "hwpx")))
        for index in range(self.profile.medium):
            rng = self.rng("medium", index)
            task = TASKS[index % len(TASKS)]
            year = rng.choice(self.profile.years)
            kind, exts = kinds[index % len(kinds)]
            when = datetime(year, rng.randint(1, THIS_MONTH), rng.randint(2, 27), 11, 0)
            title = f"{year}년 {rng.choice(task.aliases)} {kind}"
            doc = self.long_doc(rng, task, year, title, self.profile.medium_chars, when)
            folder = rng.choice((f"01_업무별/{task.name}/참고자료", f"전임자 인계/★중요/{task.name}",
                                 f"02_연도별/{year}/보고서 모음", "편람·지침"))
            self.emit_doc(self.place(folder, title, rng.choice(exts)), "medium", doc,
                          task=task.name, year=year, month=when.month)

    # 4. 큰 파일 -----------------------------------------------------
    def large(self) -> None:
        scale = self.profile.scale
        last = self.profile.years[-1]
        civil = next(task for task in TASKS if task.name == "민원처리")

        # 한 시트가 셀 상한을 넘는 대장. 상한까지만 읽고 '부분'으로 알려야 한다.
        rows = max(60, int(4000 * scale))
        columns = ("접수일", "접수번호", "유형", "행정동", "처리부서", "처리결과")
        ledger_years = self.profile.years[-3:]
        def ledger(path: Path) -> None:
            import openpyxl
            wb = openpyxl.Workbook(write_only=True)
            for year in ledger_years:
                rng = self.rng("ledger", year)
                sheet = wb.create_sheet(f"{year}년")
                sheet.append(list(columns))
                count = rows if year == last else max(40, rows // 12)
                for number in range(1, count + 1):
                    sheet.append([f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                                  f"{year}-{number:05d}", rng.choice(civil.items), rng.choice(_DONGS),
                                  rng.choice(_TEAMS), rng.choice(("처리완료", "이첩", "처리 중", "반려"))])
            wb.save(str(path))
        partial = (rows + 1) * len(columns) > MAX_CELLS_PER_SHEET
        self.emit(self.place("01_업무별/민원처리", f"민원처리대장({ledger_years[0]}~{last})", "xlsx"),
                  "large", ledger, when=datetime(last, 9, 1, 10, 0),
                  expect=["partial"] if partial else ["ok"], task="민원처리", year=last,
                  chars=min(rows * 38, 150_000) + rows // 12 * 2 * 38)
        # CP949로 내보낸 원시 측정값 — 빈 줄 없이 줄만 수천 개다.
        lines = max(80, int(3000 * scale))
        rng = self.rng("rawcsv")
        raw = "채수일,지점,탁도(NTU),잔류염소(mg/L),pH,판정\n" + "\n".join(
            f"{last - 1}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d},{rng.choice(_DONGS)} 배수지,"
            f"0.{rng.randint(5, 48):02d},0.{rng.randint(20, 95)},{rng.randint(65, 82) / 10},적합"
            for _ in range(lines))
        self.emit(self.place("수질검사 원자료", f"수질검사_원시데이터_{last - 1}_전체", "csv"), "large",
                  lambda path: path.write_bytes(raw.encode("cp949")), when=datetime(last, 1, 8, 9, 0),
                  chars=len(raw), task="수질검사원자료", year=last - 1)
        manual = self.rng("manual")
        task = TASKS[0]
        for title, ext, chars in (
            ("상수도 업무편람(전체본)", "pdf", int(110_000 * scale)),
            ("행정사무감사 요구자료 총괄(전 부서 취합본)", "docx", int(90_000 * scale)),
            (f"{last}년 주요업무계획 보고(전체)", "pptx", int(30_000 * scale)),
        ):
            doc = self.long_doc(manual, task, last, title, max(chars, 2_000), datetime(last, 2, 3, 14, 0), 2)
            self.emit_doc(self.place("편람·지침", title, ext), "large", doc, year=last)
        # 수백 행의 표. 첫 열은 세로로 병합돼 있다.
        rng = self.rng("budget")
        count = max(20, int(400 * scale))
        table = [["정책사업", "세부사업", "예산액", "전년도 예산액", "증감", "산출 근거"]]
        for number in range(count):
            amount, before = rng.randint(10, 9000) * 100, rng.randint(10, 9000) * 100
            table.append([f"정책사업 {number // 5 + 1}", f"세부사업 {number + 1} {rng.choice(_DONGS)} 관로 정비",
                          f"{amount:,}", f"{before:,}", f"{amount - before:,}",
                          f"{rng.randint(50, 900)}m × {rng.randint(100, 900)}천원"])
        budget = Doc(f"{last}년 예산서 세부사업설명서",
                     [("h1", f"{last}년 예산서 세부사업설명서"), ("p", f"작성일: {last - 1}. 11. 20."),
                      ("table", table, "천원")], datetime(last - 1, 11, 20, 15, 0))
        self.emit(self.place("01_업무별/예산", budget.title, "hwpx"), "large",
                  lambda path: _write_hwpx(path, budget, merge_first=5), when=budget.when,
                  chars=budget.chars, year=last)
        # 용량은 크고 글은 적은 것 — 사진대지와 스캔본.
        rng = self.rng("images")
        pages = self.profile.scan_pages
        size = self.profile.image_px
        base = Doc("현장점검 사진대지", [("h1", f"{last}년 해빙기 시설물 현장점검 사진대지"),
                                  ("p", f"작성일: {last}. 4. 9."), ("p", "제출처: 안전총괄과")],
                   datetime(last, 4, 9, 16, 0))
        base.images = [_jpeg(rng, size) for _ in range(max(2, pages // 2))]
        self.emit_doc(self.place("01_업무별/시설물안전점검/사진", f"{last} 해빙기 현장점검 사진대지", "docx"),
                      "large", base, expect=["partial"], year=last)
        self.emit_doc(self.place("01_업무별/시설물안전점검/사진", f"{last} 점검결과 보고(사진 포함)", "pptx"),
                      "large", base, expect=["partial"], year=last)
        scan = Doc("준공도면 스캔본", [], datetime(last - 2, 6, 30, 10, 0))
        scan.images = [_jpeg(rng, size) for _ in range(pages)]
        self.emit_doc(self.place("01_업무별/급수공사/준공서류", "준공도면_스캔본", "pdf"), "large", scan,
                      expect=["empty"], year=last - 2)
        cover = Doc("용역 계약서", [("h1", f"{last - 1}년 관망 기술진단 용역 계약서"),
                               ("p", f"계약일자: {last - 1}. 5. 2."), ("p", "계약금액: 184,500천원"),
                               ("p", "이하 계약서 본문은 날인본을 스캔하여 붙임.")], datetime(last - 1, 5, 2, 11, 0))
        cover.images = [_jpeg(rng, size) for _ in range(max(2, pages // 3))]
        self.emit_doc(self.place("01_업무별/급수공사/계약서", f"{last - 1} 관망 기술진단 용역 계약서(날인본)", "pdf"),
                      "large", cover, expect=["partial"], year=last - 1)
        for number in range(self.profile.full_text_docs):
            rng = self.rng("full", number)
            doc = self.long_doc(rng, TASKS[number % len(TASKS)], last, f"회의록 전체 모음 {number + 1}",
                                MAX_CHARS + 200_000, datetime(last, 3, 3, 9, 0), 1000)
            self.emit_doc(self.place("회의록 모음", doc.title, ("txt", "pdf", "docx")[number % 3]), "large",
                          doc, year=last, reaches_char_limit=True)

    # 5. 깨지고 이상한 파일 -------------------------------------------
    def edge_cases(self) -> None:
        rng = self.rng("edge")
        task = TASKS[10]
        when = datetime(2024, 10, 14, 13, 20)
        base, _facts = self.step_doc(task, "안전점검", 2024, 10, 14, "동절기 대비 점검 계획")
        folder = "99_기타/문제 파일"
        def raw(name: str, ext: str, data: bytes, expect: list[str] | None, **info) -> str:
            return self.emit(self.place(info.pop("folder", folder), name, ext), "edge",
                             lambda path: path.write_bytes(data), when=when, expect=expect, **info)

        def valid(ext: str) -> bytes:
            out = self.out / f".tmp-edge.{ext}"   # 점으로 시작하는 이름은 훑지 않는다
            out.parent.mkdir(parents=True, exist_ok=True)
            _write(out, base)
            data = out.read_bytes()
            out.unlink()
            return data
        # 빈 파일과 손상
        raw("빈 문서", "docx", b"", ["failed"])
        raw("빈 메모", "txt", b"", ["empty"])
        raw("빈 계약서", "pdf", b"", ["failed"])
        xlsx = valid("xlsx")
        raw("내려받다 끊긴 집계표", "xlsx", xlsx[: len(xlsx) * 3 // 5], ["failed"])
        raw("확장자만 바꾼 메모", "docx", "이 파일은 사실 메모장 글이다.".encode("utf-8"), ["failed"])
        hwpx = valid("hwpx")
        raw("전송 중 깨진 공문", "hwpx", hwpx[:200] + rng.randbytes(300), ["failed"])
        raw("잘린 스캔", "pdf", valid("pdf")[:700], ["failed", "empty", "partial"])

        # 암호
        self.emit(self.place(folder, "인사자료(암호)", "pdf"), "edge",
                  lambda path: _write_pdf(path, base, password="1234"), when=when, expect=["encrypted"])
        self.emit(self.place(folder, "평정결과(암호)", "hwpx"), "edge",
                  lambda path: _write_hwpx(path, base, extra={"META-INF/encryption.xml": "<encryption/>"}),
                  when=when, expect=["encrypted"])
        self.emit(self.place(folder, "본문 없는 한글문서", "hwpx"), "edge",
                  lambda path: _write_hwpx(path, base, sections=False), when=when, expect=["unsupported"])
        # 구형 형식 — 읽지 못한다고 분명히 알려야 한다.
        ole = bytes.fromhex("d0cf11e0a1b11ae1") + rng.randbytes(4096)
        for name, ext in (("2011 급수조례 해설", "doc"), ("2012 요금표", "xls"), ("2010 업무보고", "ppt")):
            raw(name, ext, ole, ["unsupported"], folder="옛날 자료(2013 이전)")
        raw("한글97 문서", "hwp", b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + rng.randbytes(2000),
            ["unsupported"], folder="옛날 자료(2013 이전)")
        raw("깨진 한글문서", "hwp", ole, ["failed", "unsupported"], folder="옛날 자료(2013 이전)")

        # 글자 부호와 줄 모양
        memo = "\n".join(base.lines())
        raw("메모장 ANSI 저장본", "txt", memo.encode("cp949"), ["ok"])
        raw("메모장 유니코드 저장본", "txt", b"\xff\xfe" + memo.encode("utf-16-le"), ["ok"],
            note="UTF-16. 읽기는 하지만 글자가 깨질 수 있다 — 본문이 한글로 나오는지 따로 본다.")
        raw("세미콜론 구분 내보내기", "csv", memo.replace("\t", ";").encode("cp949"), ["ok"])
        raw("서버 접속기록", "log", ("2024-10-14 13:20:01 " + rng.randbytes(15_000).hex() + "\n").encode(), ["ok"],
            note="빈칸 없는 3만 자 한 줄 — 글자 수로 잘라야 한다.")
        long_line = Doc("줄바꿈 없는 보고서", [("p", self.paragraph(rng, task, 2024, 420))], when)
        self.emit_doc(self.place(folder, "줄바꿈 없이 쓴 보고서", "docx"), "edge", long_line)
        wide = Doc("가로로 긴 표", [("table", [[f"{month}월 {item}" for month in range(1, 13) for item in
                                            ("계획", "실적", "누계", "전년", "증감", "달성률", "비고", "담당", "확인", "결재")]]
                                      + [[str(rng.randint(0, 99999)) for _ in range(120)] for _ in range(40)], "")], when)
        self.emit_doc(self.place(folder, "월별 실적 총괄(가로 120열)", "xlsx"), "edge", wide)
        blank = Doc("빈 서식", [], when)
        self.emit_doc(self.place(folder, "빈 서식", "xlsx"), "edge", blank, expect=["ok", "empty"])
        picture = Doc("표지", [], when)
        picture.images = [_jpeg(rng, (240, 320))]
        self.emit(self.place(folder, "그림만 있는 발표자료", "pptx"), "edge",
                  lambda path: _write_pptx(path, Doc("", [], when, images=picture.images)), when=when,
                  expect=["empty"])
        self.emit_doc(self.place(folder, "매크로 통합문서", "xlsm"), "edge", base)

        # 시점 단서가 어긋나거나 없는 문서
        short = Doc("연도 두 자리", [("h1", "안전점검 결과 송부"), ("p", "작성일: '24. 11. 5."),
                                ("p", "제출기한: 11. 12.(화)"), ("p", self.paragraph(rng, task, 2024))], when)
        self.emit_doc(self.place(folder, "안전점검 결과 송부('24.11.5)", "hwpx"), "edge", short,
                      note="연도가 두 자리다.")
        undated = Doc("인계 메모", [("p", "부서 안에서 구두로 인계받은 내용을 적어 둔다. 날짜 표기 없음."),
                                ("p", "배수지 점검은 팀장에게 먼저 보고하고, 보수가 필요하면 시설팀과 협의한다.")], when)
        self.emit_doc(self.place("참고자료/타부서/받은 자료/임시/정리 전/확인 필요/다시 볼 것", "제목 없음", "txt"),
                      "edge", undated, mtime=datetime(2021, 2, 2, 2, 2), note="시점 단서가 수정일뿐이다.")
        typo = Doc("오타 연도", [("h1", "2204년 안전점검 계획"), ("p", "작성일: 2204. 3. 5."),
                              ("p", self.paragraph(rng, task, 2024))], when)
        self.emit_doc(self.place(folder, "2204년 안전점검 계획(연도 오타)", "docx"), "edge", typo)

        # 이름이 까다로운 파일
        names = (
            "★★긴급★★ [회신요망] 2024년도 하반기 시설물 안전점검 결과 및 보수·보강 조치계획 제출 알림(안전총괄과-12345, 2024.11.20.) 붙임 포함 최종본",
            "안전점검 & 보수계획 #3 (100% 완료), 최종",
            "점검결과 2024．11．（전각문자）",
            unicodedata.normalize("NFD", "맥에서 보낸 안전점검 결과 접수"),
            "report_final_FINAL(2)",
        )
        for name in names:
            self.emit_doc(self.place("99_기타/이름이 이상한 파일", name, "docx"), "edge", base)
        # 같은 이름, 다른 내용
        for number, year in enumerate((2022, 2023, 2024)):
            other, _facts = self.step_doc(task, "안전점검", year, 4, 11 + number, "해빙기 점검 결과보고")
            self.emit_doc(self.place(f"02_연도별/{year}/붙임", "붙임1", "hwpx"), "edge", other, year=year)
        # 훑지 말아야 하는 것. 원장에는 적되 문서로 세지 않는다.
        for rel, data in (("99_기타/~$점검 계획.docx", b"lock"), ("99_기타/desktop.ini", b"[.ShellClassInfo]"),
                          ("99_기타/점검 계획 - 바로 가기.lnk", b"L\x00\x00\x00"),
                          (".휴지통/지운 문서.txt", "지운 문서".encode()),
                          ("99_기타/작업 중.tmp", b"tmp")):
            self.emit(rel, "excluded", lambda path, d=data: path.write_bytes(d), when=when, expect=None)

    def non_documents(self) -> None:
        small = (max(160, self.profile.image_px[0] // 2), max(120, self.profile.image_px[1] // 3))
        for number in range(self.profile.photos):
            # 사진마다 난수를 따로 둔다 — 이미 있는 파일을 건너뛰어도 다음 이름이 달라지지 않게.
            rng = self.rng("photo", number)
            when = datetime(rng.choice(self.profile.years), rng.randint(1, THIS_MONTH), rng.randint(1, 28), 15, 0)
            name = rng.choice((f"IMG_{rng.randint(1000, 9999)}", f"KakaoTalk_{when:%Y%m%d}_{number:03d}",
                               f"현장사진 {number + 1}"))
            self.emit(self.place(f"현장사진/{when.year}", name, rng.choice(("jpg", "jpg", "JPG", "jpeg"))),
                      "non_document", lambda path, r=rng: path.write_bytes(_jpeg(r, small)), when=when)
        when = datetime(2024, 6, 3, 10, 0)
        rng = self.rng("binaries")

        def archive(path: Path) -> None:
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("안에 든 문서.txt", "압축 파일 안의 문서는 읽지 않는다.")
        # ZIP은 내부 파일 이름만 읽으므로 내용 전체를 읽은 문서로 기대하지 않는다.
        self.emit("99_기타/2024 제출자료 묶음.zip", "archive", archive, when=when, expect=["partial"])
        for rel, head, size in (("99_기타/뷰어 설치.exe", b"MZ", 300_000),
                                ("현장사진/누수 복구 영상.mp4", b"\x00\x00\x00\x18ftypmp42", 2_000_000),
                                ("99_기타/조직도.png", b"\x89PNG\r\n\x1a\n", 40_000)):
            data = head + rng.randbytes(int(size * min(self.profile.scale * 2, 1.0)))
            self.emit(rel, "non_document", lambda path, d=data: path.write_bytes(d), when=when)

    def oversize(self) -> None:
        """크기 상한을 넘는 내보내기 파일. 열지 않고 '너무 큼'으로 끝나야 한다."""
        line = ("2023-12-31,가-000000,중앙동,가정용,20,13,15400,납부\n" * 2000).encode("cp949")
        def dump(path: Path) -> None:
            with path.open("wb") as handle:
                written = 0
                while written <= MAX_BYTES + 1024 * 1024:
                    handle.write(line)
                    written += len(line)
        self.emit("요금시스템 추출/요금시스템_전체추출_20231231.csv", "large", dump,
                  when=datetime(2024, 1, 2, 9, 30), expect=["too_large"])

    # 실행 -----------------------------------------------------------
    def run(self) -> dict:
        self.task_documents()
        self.weekly()
        self.daily()
        self.medium()
        self.large()
        self.edge_cases()
        self.non_documents()
        if self.profile.oversize:
            self.oversize()
        documents = [entry for entry in self.entries if entry["document"] and entry["kind"] != "excluded"]
        return {
            "note": "app.tools.make_stress_fixtures가 만든 합성 자료의 정답 원장. 실제 기관 자료가 아니다.",
            "seed": self.seed,
            "files": len(self.entries),
            "documents": len(documents),
            "bytes": sum(entry["bytes"] for entry in self.entries),
            "chars": self.chars,
            "by_extension": dict(sorted(Counter(Path(e["path"]).suffix.lower() for e in self.entries).items())),
            "by_kind": dict(sorted(Counter(e["kind"] for e in self.entries).items())),
            "tasks": sorted({e["task"] for e in self.entries if e.get("task")}),
            "questions": self.questions,
            "entries": self.entries,
        }


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(prog="make_stress_fixtures",
                                     description="과부하·시연용 합성 자료를 만든다.")
    parser.add_argument("out", nargs="?", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="hard")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--manifest", type=Path, default=None,
                        help="정답 원장 경로. 출력폴더 밖이어야 한다.")
    parser.add_argument("--force", action="store_true", help="이미 있는 파일도 다시 만든다")
    parser.add_argument("--no-oversize", action="store_true", help="200MB를 넘는 파일은 만들지 않는다")
    args = parser.parse_args(argv)
    profile = PROFILES[args.profile]
    if args.no_oversize:
        profile = replace(profile, oversize=False)
    manifest_path = args.manifest or (
        DEFAULT_MANIFEST if args.out == DEFAULT_OUT else args.out.with_name(args.out.name + "_manifest.json"))
    if args.out.resolve() in manifest_path.resolve().parents:
        print("정답 원장은 출력폴더 밖에 두어야 합니다 — 안에 두면 자료로 훑습니다.", file=sys.stderr)
        return 2
    generator = Generator(args.out, profile, args.seed, args.force)
    manifest = generator.run()
    manifest["profile"] = args.profile
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"합성 자료 {manifest['files']:,}건 (문서 {manifest['documents']:,}건, "
          f"{manifest['bytes'] / 1024 / 1024:,.1f}MB, 본문 약 {manifest['chars']:,}자)")
    print(f"  새로 쓴 파일 {generator.written:,}건 · 업무 {len(manifest['tasks'])}종")
    print("  " + " · ".join(f"{ext or '(없음)'} {n}" for ext, n in manifest["by_extension"].items()))
    print(f"출력폴더  {args.out.resolve()}")
    print(f"정답 원장 {manifest_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
