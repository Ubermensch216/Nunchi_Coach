"""자료 단위(공문 건·폴더 묶음·낱장)와 명함."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import gongmun_facts, units
from app.db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


def doc(db, source, rel, text="", digest=None, year=2025):
    path = f"D:/전임자/{rel}"
    doc_id = db.con.execute(
        "INSERT INTO documents (source_id, path, filename, ext, hash, eff_year, parse_status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'ok')",
        (source, path, rel.rsplit("/", 1)[-1], Path(rel).suffix, digest or rel, year)).lastrowid
    if text:
        db.con.execute("INSERT INTO document_sections(doc_id, ordinal, kind, locator, text) VALUES (?, 0, 'paragraph', 'p1', ?)",
                       (doc_id, text))
        db.con.execute("INSERT INTO chunks(doc_id, ordinal, locator, text) VALUES (?, 0, 'p1', ?)", (doc_id, text))
    return doc_id


BODY = ("제목 적극행정 실행계획 제출 요청\n1. 실행계획을 수립하고자 하오니 부서별 계획을 작성하여 제출하여 주시기 바랍니다.\n"
        "2. 제출 기한: 2025. 4. 15.까지")


def test_units_letters_folders_and_loose(db):
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES ('D:/전임자', 'local')").lastrowid
    body = doc(db, source, "공문/0123456789ab-[본문] 적극행정 실행계획 제출 요청.pdf", BODY)
    attach = doc(db, source, "공문/0123456789ab-붙임1 서식.hwp")
    same = doc(db, source, "내문서/실행계획 서식.hwp", digest="공문/0123456789ab-붙임1 서식.hwp")
    titled = doc(db, source, "내문서/적극행정 실행계획 초안.hwp")
    f1 = doc(db, source, "예산/본예산/요구서.xlsx", "예산 요구서")
    f2 = doc(db, source, "예산/본예산/집행계획.xlsx", "집행 계획")
    single = doc(db, source, "기타/메모.txt", "메모")
    top = doc(db, source, "공지.txt", "공지")
    gongmun_facts.analyze_bodies(db)

    built = units.build(db)
    by_kind = {k: [u for u in built if u.kind == k] for k in ("letter", "folder", "loose")}
    assert len(by_kind["letter"]) == 1 and len(by_kind["folder"]) >= 1
    letter = by_kind["letter"][0]
    assert letter.docs[body][0] == "body" and letter.docs[attach][0] == "attachment"
    assert letter.docs[same] == ("linked", units.SAME_BASIS)
    assert letter.docs[titled] == ("linked", units.TITLE_BASIS)
    assert any(f["kind"] == "schedule" and f["due"] == "2025-04-15" for f in letter.card["facts"])
    budget = next(u for u in by_kind["folder"] if f1 in u.docs)
    assert f2 in budget.docs and budget.label == "본예산" and budget.card["folder"] == "예산/본예산"
    assert budget.card["excerpts"]
    loose_ids = {d for u in by_kind["loose"] for d in u.docs}
    assert {single, top} <= loose_ids
    # 모든 문서는 정확히 한 단위에 속한다.
    all_ids = [d for u in built for d in u.docs]
    assert sorted(all_ids) == sorted(set(all_ids)) and set(all_ids) == {body, attach, same, titled, f1, f2, single, top}


def test_ambiguous_title_is_not_linked(db):
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES ('D:/전임자', 'local')").lastrowid
    for rel in ("a/[본문] 적극행정 실행계획 요청.pdf", "a/[본문] 적극행정 실행계획 결과.pdf"):
        doc(db, source, rel, BODY)
    extra = doc(db, source, "b/적극행정 실행계획 서식.hwp")
    built = units.build(db)
    assert all(extra not in u.docs or u.kind != "letter" for u in built)


def test_save_and_load_roundtrip_replaces(db):
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES ('D:/전임자', 'local')").lastrowid
    doc(db, source, "예산/요구서.xlsx", "예산")
    doc(db, source, "예산/집행.xlsx", "집행")
    assert units.save(db)["folder"] == 1
    assert units.save(db)["folder"] == 1
    loaded = units.load(db)
    assert len(loaded) == 1 and loaded[0]["card"]["folder"] == "예산" and len(loaded[0]["doc_ids"]) == 2
    assert units.unit_of(db, loaded[0]["doc_ids"][0]) == loaded[0]["id"]
