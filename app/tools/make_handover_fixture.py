"""실제 인수인계 폴더의 '꼴'을 본뜬 합성 자료를 만든다.

    python -m app.tools.make_handover_fixture <빈 폴더>

make_fixtures가 만드는 표본은 `2025_행정사무감사_제출자료.docx`처럼 파일명에 업무가 들어 있고
폴더가 연도별로 나뉜 **깨끗한** 자료다. 실제 전임자 폴더는 그렇지 않았다(2026-10-12 [적극행정]
자료 1,228개로 확인). 이 표본은 그 꼴만 옮긴다. 내용·이름·날짜는 모두 지어낸 것이다.

  · 업무 폴더 아래에 `1. 선발계획 → 2. 부서제출 → …` 번호 폴더가 순서를 말한다
  · `[0. old]\\[2023년도]\\[…]` 아래에 지난 회차가 통째로 있고, 폴더에 `[2023-06-01]` 기안일이 있다
  · `old`·`bak`·`ref`·`부서제출` 폴더가 이전 판·참고·받은 자료를 가른다
  · 계획서에 추진일정이 `… : 10. 18.(금) 한` 꼴로 적혀 있다
  · 전임자가 남긴 메모(txt), 스캔한 의결서(글자 없는 PDF), 화면 캡처(그림), 압축파일
  · 한글의 XML 저장본(.hwp 이름의 HWPML)
  · 폴더에 들지 않은 낱장 — 이름에 업무가 있는 것과 없는 것

여기 만든 폴더로 분석을 끝까지 돌려 폴더 구조·시점·일정·갈래가 바르게 읽히는지를 시험한다
(tests/test_handover_shape.py).
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from .console import force_utf8
from .make_fixtures import _write_hwpx

PREFIX = "환경정책"

_HWPML = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE HWPML [
\t<!ENTITY nbsp\t"&#160;">
]>
<HWPML Style="embed" SubVersion="10.0.0.0" Version="2.8"><HEAD><DOCSUMMARY><TITLE>{title}</TITLE></DOCSUMMARY></HEAD>
<BODY><SECTION>{paras}</SECTION></BODY></HWPML>"""


def build(root: Path) -> dict[str, Path]:
    """root 아래에 폴더 구조를 만든다. 이름으로 찾아 쓸 경로들을 돌려준다."""
    root = Path(root)
    made: dict[str, Path] = {}

    def doc(key: str, relative: str, body: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".txt":
            path.write_text(body, encoding="utf-8")
        else:
            _write_hwpx(path, body)
        made[key] = path

    award = f"2024년 {PREFIX} 우수부서"
    # ── 우수부서 선발 ─────────────────────────────────────────────
    doc("plan_h2", rf"{award}\하반기\1. 선발계획\2024년 하반기 우수부서 선발 계획.hwpx",
        "2024년 하반기 우수부서 선발 계획\n추진일정\n"
        "우수부서 추천(전부서) : 10. 18.(금) 한\n1차 심사평가(후보 선정) : 10. 25.(금) 한\n"
        "위원회 개최(우수부서 선정) : 11. 15.(금) 한\n공적기간 : 2024. 5. 1. ~ 2024. 10. 31.")
    doc("received", rf"{award}\하반기\2. 부서제출\건설과 추천서.hwpx",
        "2024년 하반기 우수부서 추천서 건설과\n사업비 : 34억원\n담당자 : 건설과 김하나")
    doc("round1", rf"{award}\하반기\3. 1차 심사\2024년 하반기 1차 심사 총괄표.hwpx",
        "2024년 하반기 1차 심사 총괄표\n심사위원 점수 합계")
    doc("previous", rf"{award}\하반기\3. 1차 심사\old\이전 총괄표.hwpx", "이전 판 총괄표\n사업비 : 99억원")
    doc("meeting", rf"{award}\하반기\4. 위원회 개최\2024년 하반기 위원회 시나리오.hwpx",
        "위원회 시나리오\n일 시 : 2024. 11. 15.(금) 15:00\n개회 및 위원 소개")
    doc("plan_h1", rf"{award}\상반기\2024년 상반기 우수부서 선발 계획.hwpx",
        "2024년 상반기 우수부서 선발 계획\n추진일정\n우수부서 추천(전부서) : 6. 17.(월) 한\n"
        "위원회 개최(우수부서 선정) : 6. 28.(금) 한")
    doc("memo", rf"{award}\우수부서선발－주의사항.txt",
        "1. 발표자 시간제한 타이머 운영\n2. 좌석순서 확인\n3. 준비물 : 채점표, 계산기, 생수")
    old_award = rf"[0. old]\[2023년도]\[{PREFIX}위원회 우수부서 선발]"
    doc("old_plan", rf"{old_award}\1. [2023-06-01] 2023년 상반기 우수부서 선발 계획\2023년 상반기 우수부서 선발 계획.hwpx",
        "2023년 상반기 우수부서 선발 계획\n추진일정\n우수부서 추천(전부서) : 6. 9.(금) 한\n"
        "위원회 개최(우수부서 선정) : 6. 30.(금) 한")
    doc("old_notice", rf"{old_award}\2. [2023-06-15] 1차 심사평가 계획\2023년 상반기 1차 심사평가 계획.hwpx",
        "2023년 상반기 1차 심사평가 계획\n1차 심사평가 : 6. 22.(목) 한")
    doc("incentive", f"2024년 {PREFIX} 우수부서 인센티브 부여계획.hwpx",
        "인센티브 부여계획\n인센티브 부여 요청(총무과) : 7월 한")

    # ── 실행계획 ──────────────────────────────────────────────────
    doc("exec_plan", rf"2024년 {PREFIX} 실행계획\市 공문\2024년도 실행계획 제출 요청.hwpx",
        "2024년도 실행계획 제출 요청\n1. 실행계획을 수립하여 2024. 6. 14.(금)까지 제출하여 주시기 바랍니다.")
    doc("exec_draft", rf"2024년 {PREFIX} 실행계획\bak\2024년 실행계획_결재전.hwpx", "실행계획 결재전 초안")
    doc("exec_final", rf"2024년 {PREFIX} 실행계획\2024년 실행계획.hwpx", "2024년 실행계획\n추진목표 및 추진전략")
    doc("exec_old", rf"[0. old]\[2023년도]\[{PREFIX} 실행계획]\2023년 실행계획.hwpx",
        "2023년 실행계획\n실행계획 전 직원 공유 : 2023. 6. 16.(금) 한")

    # ── 위원회 ────────────────────────────────────────────────────
    committee = f"{PREFIX} 위원회"
    doc("committee_plan", rf"{committee}\2024년 제3회 위원회\2024년 제3차 위원회 개최 계획.hwpx",
        "2024년 제3차 위원회 개최 계획\n일 시 : 2024. 6. 28.(금) 10:00")
    scan = root / committee / "2024년 제3회 위원회" / "2024년 제3차 위원회 심의의결서.pdf"
    _blank_pdf(scan)
    made["scan"] = scan
    capture = root / committee / "[쪽지] 위원회 관련.png"
    _blank_png(capture)
    made["capture"] = capture
    evidence = root / committee / "2024년 제3회 위원회" / "증빙자료.zip"
    with zipfile.ZipFile(evidence, "w") as package:
        package.writestr("재무과 증빙/매매계약서.pdf", b"%PDF-1.4")
        package.writestr("재무과 증빙/추진사항 보고.hwp", b"x")
    made["zip"] = evidence

    # ── 법령 (한글의 XML 저장본) ──────────────────────────────────
    law = root / f"{PREFIX} 법령" / f"{PREFIX} 운영 조례.hwp"
    law.parent.mkdir(parents=True, exist_ok=True)
    paras = "".join(f"<P><TEXT><CHAR>{line}</CHAR></TEXT></P>" for line in (
        f"{PREFIX} 운영 조례", "제1조(목적) 이 조례는 환경정책의 적극적 추진을 위한 사항을 정한다.",
        "제2조(위원회) 위원의 임기는 3년으로 하며 한 차례 연임할 수 있다."))
    law.write_text(_HWPML.format(title=f"{PREFIX} 운영 조례", paras=paras), encoding="utf-8")
    made["law"] = law
    doc("law_guide", rf"{PREFIX} 법령\운영규정 해설.hwpx", "운영규정 해설\n제정 이유와 주요 내용")

    # ── 교육 ──────────────────────────────────────────────────────
    doc("edu_plan", r"교육\2024년 직장교육\교육 실시 계획.hwpx",
        "2024년 교육 실시 계획\n일 시 : 2024. 10. 24.(목) 09:30\n교육 종료 후 상시학습인정 : 11월 초 한")
    doc("edu_pay", r"교육\2024년 직장교육\강사수당 견적서.hwpx", "견적서\n합 계 : 513,000원")

    # ── 폴더에 들지 않은 낱장 ─────────────────────────────────────
    doc("loose_unclear", "담당자 명단.hwpx", "구군 담당자 명단\n기획실 김명진")
    return made


def _blank_pdf(path: Path) -> None:
    """글자 레이어가 없는 PDF — 스캔한 문서처럼 읽을 글자가 없다."""
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        document.new_page()
        document.save(str(path))


def _blank_png(path: Path) -> None:
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 300), False).save(str(path))


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0].startswith("-"):
        print("사용법: python -m app.tools.make_handover_fixture <빈 폴더>")
        return 2
    target = Path(args[0])
    if target.exists() and any(target.iterdir()):
        print(f"비어 있지 않은 폴더입니다: {target}")
        return 2
    made = build(target)
    print(f"{target}에 파일 {sum(1 for p in target.rglob('*') if p.is_file())}개를 만들었습니다.")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
