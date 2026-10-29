"""공문으로 업무 찾기 — 공문이 업무의 뼈대가 되고 PC 파일이 거기에 붙는다.

docs/공문기준_업무분석_계획서_2026-10-29.md P3·P4a. 합성 자료다(실제 온나라 공문이 아니다).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.discover import discover, has_official_tasks
from app.core import official
from app.db import Database

ONNARA = r"D:\프로젝트\onnara\run1\files"
LOCAL = r"D:\자료"


@pytest.mark.parametrize("title, label, core", [
    ("2025년 적극행정 실행계획 제출 요청", "적극행정 실행계획", "적극행정실행계획"),
    ("적극행정 실행계획 제출(회신)", "적극행정 실행계획", "적극행정실행계획"),
    ("[긴급] 2024년 하반기 민원 처리 실태 점검 결과 보고", "민원 처리 실태 점검", "민원처리실태점검"),
    ("교육 안내", "교육", ""),                       # 너무 짧은 알맹이로는 업무를 세우지 않는다
])
def test_title_core_keeps_the_work_and_drops_what_the_letter_does(title, label, core):
    assert official.title_label(title) == label
    assert official.title_core(title) == core


def test_document_numbers_are_matched_exactly():
    assert "기획과-1234" in official.numbers_in("1. 관련: 기획과 - 1234(2025. 3. 2.)")
    assert "기획과-1234" in official.numbers_in("붙임1_기획과-1234_계획.hwp")
    assert "기획과-12" not in official.numbers_in("관련: 기획과-1234")
    assert "기획과-12" not in official.numbers_in("관련: 교육기획과-12")
    assert official.numbers_in("제출기한 2025-09-30") == set()
    assert official.normalize_number("행정안전부 혁신과 - 100") == "혁신과-100"


@pytest.fixture
def project(tmp_path: Path):
    db = Database(tmp_path / "p.db")
    db.init()
    yield db
    db.close()


class Seed:
    def __init__(self, db: Database):
        self.db = db
        self.onnara = db.add_source(ONNARA, kind="onnara")
        self.local = db.add_source(LOCAL)
        db.con.execute("INSERT INTO collection_runs(collection_key, adapter, institution, system_id, scope)"
                       " VALUES ('k', 'saide-package-v1', 'onnara.invalid', 'onnara@onnara.invalid', '{}')")
        self.run = db.con.execute("SELECT id FROM collection_runs").fetchone()["id"]
        self.count = 0

    def doc(self, source_id: int, path: str, digest: str, body: str = "", year: int = 2024) -> int:
        self.count += 1
        doc_id = self.db.upsert_document(source_id, {
            "path": path, "filename": Path(path).name, "ext": Path(path).suffix,
            "parse_status": "ok", "hash": digest, "char_count": 400,
            "eff_year": year, "eff_date": f"{year}-03-01", "eff_date_kind": "body"})
        if body:
            self.db.con.execute("INSERT INTO chunks(doc_id, ordinal, locator, text) VALUES (?, 0, '1쪽', ?)",
                                (doc_id, body))
        return doc_id

    def letter(self, key: str, title: str, number: str = "", body: str = "", card: str = "",
               attachment: str | None = None, year: int = 2024) -> dict[str, int]:
        """공문 한 건 — 본문(과 붙임)을 수집본 폴더에 두고 출처 기록에 잇는다."""
        cursor = self.db.con.execute(
            "INSERT INTO source_records(run_id, institution, system_id, document_id, title, document_number,"
            " authored_at, task_card) VALUES (?, 'onnara.invalid', 'onnara@onnara.invalid', ?, ?, ?, ?, ?)",
            (self.run, key, title, number, f"{year}-03-01", card))
        record = cursor.lastrowid
        made = {"body": self.doc(self.onnara, rf"{ONNARA}\{key}\본문.pdf", f"{key}-body", body or title, year)}
        if attachment:
            made["attachment"] = self.doc(self.onnara, rf"{ONNARA}\{key}\붙임.hwp", attachment, year=year)
        for role, doc_id in made.items():
            digest = self.db.con.execute("SELECT hash FROM documents WHERE id=?", (doc_id,)).fetchone()["hash"]
            self.db.con.execute(
                "INSERT INTO source_assets(record_id, asset_id, role, status, doc_id, sha256)"
                " VALUES (?, ?, ?, 'downloaded', ?, ?)", (record, role, role, doc_id, digest))
        return made


def _tasks(db: Database) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for row in db.con.execute("SELECT t.name, td.doc_id FROM tasks t JOIN task_docs td ON td.task_id=t.id"):
        out.setdefault(row["name"], set()).add(row["doc_id"])
    return out


def _link(db: Database, doc_id: int) -> tuple[str, str]:
    row = db.con.execute("SELECT confidence, evidence FROM task_docs WHERE doc_id=?", (doc_id,)).fetchone()
    return row["confidence"], row["evidence"]


def test_letters_become_the_skeleton_and_pc_files_join_with_their_reason(project):
    seed = Seed(project)
    a = seed.letter("A", "2024년 적극행정 실행계획 제출 요청", "행정안전부 혁신과-100")
    b = seed.letter("B", "2024년 적극행정 실행계획 제출", "기획과-55",
                    body="1. 관련: 혁신과-100(2024. 2. 1.) 2. 우리 시 실행계획을 제출합니다.",
                    attachment="final-plan")
    c = seed.letter("C", "2025년 적극행정 실행계획 제출 요청", "행정안전부 혁신과-210", year=2025)
    d = seed.letter("D", "민원 처리 실태 점검 결과 보고", "기획과-77")
    e = seed.letter("E", "자료 송부", "기획과-81", body="1. 관련: 기획과-77 2. 점검 자료를 보냅니다.")
    f = seed.letter("F", "교육 안내", "총무과-9")

    same = seed.doc(seed.local, rf"{LOCAL}\적극행정\2024 실행계획(최종).hwp", "final-plan")
    titled = seed.doc(seed.local, rf"{LOCAL}\적극행정\적극행정 실행계획 작성요령.hwp", "guide")
    near = seed.doc(seed.local, rf"{LOCAL}\적극행정\회의자료.hwp", "meeting")
    numbered = seed.doc(seed.local, rf"{LOCAL}\민원\기획과-77_점검표.xlsx", "sheet")
    loose = seed.doc(seed.local, rf"{LOCAL}\메모.txt", "memo")

    assert has_official_tasks(project)
    result = discover(project, client=None)

    tasks = _tasks(project)
    assert set(tasks) == {"적극행정 실행계획", "민원 처리 실태 점검"}
    assert tasks["적극행정 실행계획"] == {*a.values(), *b.values(), *c.values(), same, titled, near}
    assert tasks["민원 처리 실태 점검"] == {*d.values(), *e.values(), numbered}      # 회신은 문서번호로 잇는다
    assert f["body"] not in set().union(*tasks.values())                           # 1건짜리 접수 공문은 업무가 아니다
    assert loose not in set().union(*tasks.values())                               # 자료 폴더 맨 위는 쓰지 않는다

    assert _link(project, same) == ("high", "공문 파일과 같은 내용")
    assert _link(project, numbered) == ("high", "파일 이름에 공문 문서번호")
    assert _link(project, titled) == ("medium", "파일 이름에 공문 제목")
    assert _link(project, near) == ("low", "공문과 대응된 파일과 같은 폴더")
    assert _link(project, a["body"]) == ("high", "온나라 공문 · 공문 제목")
    assert (result.named_by_official, result.matched_to_official) == (2, 4)
    rounds = {row["doc_id"]: row["round_key"] for row in project.con.execute("SELECT doc_id, round_key FROM task_docs")}
    assert rounds[c["body"]] == "2025" and rounds[a["body"]] == "2024"
    description = project.con.execute("SELECT description FROM tasks WHERE name='적극행정 실행계획'").fetchone()[0]
    assert "온나라 공문 3건(2024~2025)" in description and "이 PC의 파일 3건" in description


def test_task_cards_decide_and_different_cards_are_never_merged(project):
    seed = Seed(project)
    seed.letter("A", "2024년 성과평가 계획 알림", card="성과관리")
    seed.letter("B", "2024년 성과평가 계획 알림", card="조직진단")
    seed.letter("C", "청사 시설 점검 결과", card="청사관리")       # 단위과제가 있으면 1건도 업무다

    discover(project, client=None)
    assert set(_tasks(project)) == {"성과관리", "조직진단", "청사관리"}
    assert _link(project, 1)[1] == "온나라 공문 · 단위과제"


def test_a_file_a_person_placed_elsewhere_stays_there(project):
    seed = Seed(project)
    a = seed.letter("A", "2024년 적극행정 실행계획 제출 요청")
    seed.letter("B", "2025년 적극행정 실행계획 제출 요청", year=2025)
    project.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('사람이 만든 업무', 'user', 'approved')")
    mine = project.con.execute("SELECT id FROM tasks WHERE name='사람이 만든 업무'").fetchone()["id"]
    project.con.execute("INSERT INTO task_docs(task_id, doc_id, origin) VALUES (?, ?, 'user')", (mine, a["body"]))

    discover(project, client=None)
    tasks = _tasks(project)
    assert tasks["사람이 만든 업무"] == {a["body"]}
    assert a["body"] not in tasks["적극행정 실행계획"]


def test_a_pc_file_that_fits_two_letters_equally_is_left_alone(project):
    seed = Seed(project)
    seed.letter("A", "2024년 성과평가 계획 알림")
    seed.letter("B", "2025년 성과평가 계획 알림", year=2025)
    seed.letter("C", "2024년 조직진단 결과 보고")
    seed.letter("D", "2025년 조직진단 결과 보고", year=2025)
    both = seed.doc(seed.local, rf"{LOCAL}\성과평가 조직진단 합본.hwp", "both")

    discover(project, client=None)
    assert set(_tasks(project)) == {"성과평가", "조직진단"}
    assert both not in set().union(*_tasks(project).values())
