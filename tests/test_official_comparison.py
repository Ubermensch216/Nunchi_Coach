"""공문 ↔ PC 로컬 비교 고도화 시험 (P5 + P4a 보조).

확인할 것:
  1. 기안 초안 ↔ 등록본 판 묶음(versions.py): 공문 파일명이 '본문.pdf'여도 공문 제목·본문 겹침이 있는
     PC 초안('실행계획_초안.hwp', '실행계획_수정.hwp')과 한 판 묶음이 되고 온나라 등록본이 대표 판이 되는가
  2. 공문 중심 흐름·주기(timeline.py): 파일 복사일로 오염된 fs_mtime 대신 온나라 공문의 기안·등록일
     (authored_at)을 우선해 일한 달을 판정하는가
  3. 공문↔로컬 대조 공백 3종(continuity.py):
     - unanswered_official (요청 공문은 있으나 회신 공문·제출본 없음)
     - unregistered_final (PC에 '최종/확정/결재' 파일만 있고 대응 등록 공문 없음)
     - missing_attachment (공문에 기록된 붙임이 미수집이고 로컬에도 없음)
  4. 값 충돌 시 출처 대비(continuity.py, provenance.py): 온나라 공문(문서번호·기안일·결재상태) vs
     개인 폴더 자료(PC 파일)를 나란히 표기하는가
  5. 본문 유사도 5순위 보조 대응(discover.py): 해시·문서번호·제목·폴더가 달라도 벡터 유사도가 높으면
     '공문 본문과 내용 유사' 근거(low)로 붙는가
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.ai.discover import discover
from app.core import continuity, reconstruction as rc, timeline, versions
from app.db import Database
from app.search.vector import pack


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "compare.db")
    database.init()
    yield database
    database.close()


def _setup_sources(db: Database, tmp_path: Path) -> tuple[int, int, int]:
    onnara_src = db.add_source(tmp_path / "onnara", kind="onnara")
    local_src = db.add_source(tmp_path / "local")
    db.con.execute(
        "INSERT INTO collection_runs(collection_key, adapter, institution, system_id, scope)"
        " VALUES ('run-1', 'saide-package-v1', 'onnara.invalid', 'onnara@onnara.invalid', '{}')"
    )
    run_id = db.con.execute("SELECT id FROM collection_runs").fetchone()["id"]
    return onnara_src, local_src, run_id


def _add_doc(
    db: Database,
    source_id: int,
    path: str,
    digest: str,
    body: str,
    *,
    year: int = 2025,
    month: int = 3,
    eff_date: str | None = None,
    fs_mtime: str | None = None,
) -> int:
    eff = eff_date or f"{year}-{month:02d}-10"
    doc_id = db.upsert_document(source_id, {
        "path": path,
        "filename": Path(path).name,
        "ext": Path(path).suffix,
        "parse_status": "ok",
        "hash": digest,
        "char_count": len(body),
        "eff_year": year,
        "eff_month": month,
        "eff_date": eff,
        "eff_precision": "day",
        "eff_date_kind": "body",
        "fs_mtime": fs_mtime or f"{year}-{month:02d}-10T10:00:00",
    })
    db.replace_sections(doc_id, [("paragraph", 0, "1문단", body, "{}")])
    db.index_document(doc_id, Path(path).name, path, "", body)
    db.con.execute(
        "INSERT INTO chunks(doc_id, ordinal, locator, text) VALUES (?, 0, '1문단', ?)",
        (doc_id, body),
    )
    return doc_id


def test_draft_and_registered_official_are_grouped_with_official_as_representative(db: Database, tmp_path: Path):
    onnara_src, local_src, run_id = _setup_sources(db, tmp_path)
    shared_body = "2025년 적극행정 실행계획 중점과제 선정 및 반기별 점검 추진 방안 보고"

    cur = db.con.execute(
        "INSERT INTO source_records(run_id, institution, system_id, document_id, title, document_number,"
        " status, authored_at, task_card)"
        " VALUES (?, 'onnara.invalid', 'onnara@onnara.invalid', 'doc-1', '2025년 적극행정 실행계획 수립 보고',"
        " '혁신과-501', 'approved', '2025-03-15', '적극행정')",
        (run_id,),
    )
    rec_id = cur.lastrowid
    off_doc = _add_doc(
        db, onnara_src, str(tmp_path / "onnara" / "doc-1" / "본문.pdf"),
        "hash-off", shared_body + " 최종 결재본", year=2025, month=3, eff_date="2025-03-15",
    )
    db.con.execute(
        "INSERT INTO source_assets(record_id, asset_id, role, status, doc_id, sha256)"
        " VALUES (?, 'body', 'body', 'downloaded', ?, 'hash-off')",
        (rec_id, off_doc),
    )

    draft1 = _add_doc(
        db, local_src, str(tmp_path / "local" / "2025_적극행정_실행계획_초안.hwp"),
        "hash-d1", shared_body + " 초안 작성", year=2025, month=3, eff_date="2025-03-05",
    )
    draft2 = _add_doc(
        db, local_src, str(tmp_path / "local" / "2025_적극행정_실행계획_수정본.hwp"),
        "hash-d2", shared_body + " 과장님 검토 반영 수정", year=2025, month=3, eff_date="2025-03-12",
    )

    db.con.execute("INSERT INTO tasks(id, name, origin, status) VALUES (1, '적극행정', 'ai', 'proposed')")
    for d_id in (off_doc, draft1, draft2):
        db.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (1, ?)", (d_id,))

    made = versions.rebuild(db, 1)
    assert made == 1
    groups = versions.groups_for(db, [off_doc, draft1, draft2])
    assert len(groups) == 1
    grp = groups[0]
    assert grp["latest_doc_id"] == off_doc
    assert grp["confidence"] == "high"
    assert "온나라에 등록된 판" in grp["latest_reason"]
    reps = versions.representatives(db, [off_doc, draft1, draft2])
    assert reps[draft1] == off_doc and reps[draft2] == off_doc and reps[off_doc] == off_doc


def test_work_month_prioritizes_official_authored_at_over_copied_fs_mtime():
    # 파일 수정일(fs_mtime)이 12월(복사된 날)이어도 공문 기안일(authored_at)이 3월이면 3월로 판정한다.
    month, trusted = timeline.work_month(
        year=2025,
        month=1,
        kind="body",
        precision="day",
        fs_mtime="2025-12-28T15:00:00",
        authored_at="2025-03-14",
    )
    assert (month, trusted) == (3, True)


def test_official_local_gaps_detect_unanswered_unregistered_and_missing_attachment(db: Database, tmp_path: Path):
    onnara_src, local_src, run_id = _setup_sources(db, tmp_path)
    db.con.execute("INSERT INTO tasks(id, name, origin, status) VALUES (1, '공공데이터 품질진단', 'ai', 'proposed')")

    # 1) 요청 공문 (회신 없음) + 누락된 붙임 1건
    cur = db.con.execute(
        "INSERT INTO source_records(run_id, institution, system_id, document_id, title, document_number,"
        " status, authored_at, task_card)"
        " VALUES (?, 'onnara.invalid', 'onnara@onnara.invalid', 'req-1',"
        " '2025년 공공데이터 품질진단 실적 제출 요청', '데이터과-100', 'approved', '2025-04-02', '공공데이터')",
        (run_id,),
    )
    req_rec = cur.lastrowid
    req_doc = _add_doc(
        db, onnara_src, str(tmp_path / "onnara" / "req-1" / "본문.pdf"),
        "hash-req", "제출기한 : 2025. 4. 30.\n제출처 : 행정안전부 공공데이터정책과",
        year=2025, month=4, eff_date="2025-04-02",
    )
    db.con.execute(
        "INSERT INTO source_assets(record_id, asset_id, role, status, doc_id, sha256)"
        " VALUES (?, 'body', 'body', 'downloaded', ?, 'hash-req')",
        (req_rec, req_doc),
    )
    # 내려받지 못한 붙임 서식
    db.con.execute(
        "INSERT INTO source_assets(record_id, asset_id, role, filename, status, error)"
        " VALUES (?, 'att-1', 'attachment', '붙임1_자체점검표_서식.xlsx', 'failed', 'download timeout')",
        (req_rec,),
    )

    # 2) PC에는 다른 주제/연도의 '최종' 계획안만 있고 등록 공문은 없음 (unregistered_final)
    local_final = _add_doc(
        db, local_src, str(tmp_path / "local" / "2024년_정비계획_최종.hwp"),
        "hash-final", "2024년 자체 정비계획 최종안",
        year=2024, month=10, eff_date="2024-10-10",
    )

    for d_id in (req_doc, local_final):
        db.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (1, ?)", (d_id,))

    gaps = continuity.find_gaps(db, 1, basis=date(2025, 4, 15))
    by_kind = {g["kind"]: g for g in gaps}

    assert "unanswered_official" in by_kind
    assert "데이터과-100" in by_kind["unanswered_official"]["detail"]

    assert "unregistered_final" in by_kind
    assert "2024년_정비계획_최종.hwp" in by_kind["unregistered_final"]["detail"]

    assert "missing_attachment" in by_kind
    assert "붙임1_자체점검표_서식.xlsx" in by_kind["missing_attachment"]["detail"]


def test_value_conflict_contrasts_official_record_with_local_file(db: Database, tmp_path: Path):
    onnara_src, local_src, run_id = _setup_sources(db, tmp_path)
    db.con.execute("INSERT INTO tasks(id, name, origin, status) VALUES (1, '합동평가', 'ai', 'proposed')")

    cur = db.con.execute(
        "INSERT INTO source_records(run_id, institution, system_id, document_id, title, document_number,"
        " status, authored_at)"
        " VALUES (?, 'onnara.invalid', 'onnara@onnara.invalid', 'off-1',"
        " '2025년 합동평가 실적 제출 요청', '기획관-777', 'approved', '2025-05-02')",
        (run_id,),
    )
    rec_id = cur.lastrowid
    off_doc = _add_doc(
        db, onnara_src, str(tmp_path / "onnara" / "off-1" / "본문.pdf"),
        "h-off", "제출기한 : 2025. 5. 20.", year=2025, month=5, eff_date="2025-05-02",
    )
    db.con.execute(
        "INSERT INTO source_assets(record_id, asset_id, role, status, doc_id, sha256)"
        " VALUES (?, 'body', 'body', 'downloaded', ?, 'h-off')",
        (rec_id, off_doc),
    )

    local_doc = _add_doc(
        db, local_src, str(tmp_path / "local" / "내부검토_초안.hwp"),
        "h-loc", "제출기한 : 2025. 5. 15.", year=2025, month=5, eff_date="2025-05-01",
    )

    for d_id in (off_doc, local_doc):
        db.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (1, ?)", (d_id,))

    rc.restore(db, date(2025, 5, 10), statements=False)
    gaps = db.info_gaps(1)
    conflict_gap = next(g for g in gaps if g["kind"] == "conflict")
    assert "기획관-777" in conflict_gap["research"]
    assert "2025-05-02" in conflict_gap["research"]
    assert "개인 폴더 자료(PC 파일)" in conflict_gap["research"]


def test_content_similarity_fallback_attaches_local_file_to_official_task(db: Database, tmp_path: Path):
    onnara_src, local_src, run_id = _setup_sources(db, tmp_path)

    for idx, num in ((1, "혁신과-10"), (2, "혁신과-11")):
        cur = db.con.execute(
            "INSERT INTO source_records(run_id, institution, system_id, document_id, title, document_number,"
            " status, authored_at, task_card)"
            " VALUES (?, 'onnara.invalid', 'onnara@onnara.invalid', ?, ?, ?, 'approved', '2025-03-10', '적극행정')",
            (run_id, f"off-{idx}", f"2025년 적극행정 위원회 운영 안내 {idx}", num),
        )
        rec_id = cur.lastrowid
        d_id = _add_doc(
            db, onnara_src, str(tmp_path / "onnara" / f"off-{idx}" / "본문.pdf"),
            f"h-off-{idx}", "적극행정 면책 및 사전컨설팅 감사 운영 지침 안내", year=2025, month=3,
        )
        db.con.execute(
            "INSERT INTO source_assets(record_id, asset_id, role, status, doc_id, sha256)"
            " VALUES (?, 'body', 'body', 'downloaded', ?, ?)",
            (rec_id, d_id, f"h-off-{idx}"),
        )
        db.save_doc_embedding(d_id, "bge-m3", 4, pack([0.9, 0.1, 0.0, 0.0]), 100)

    # 파일명·문서번호·폴더에는 '적극행정' 단서가 없지만 본문 임베딩이 공문과 매우 유사한 PC 파일
    similar_local = _add_doc(
        db, local_src, str(tmp_path / "local" / "별도폴더" / "사전컨설팅_검토의견서.hwp"),
        "h-sim", "면책 요건 검토 및 사전컨설팅 감사 요청에 대한 의견", year=2025, month=3,
    )
    db.save_doc_embedding(similar_local, "bge-m3", 4, pack([0.88, 0.12, 0.0, 0.0]), 100)

    res = discover(db, client=None)
    assert res.named_by_official == 1
    row = db.con.execute(
        "SELECT confidence, evidence FROM task_docs WHERE doc_id = ?", (similar_local,)
    ).fetchone()
    assert row is not None
    assert (row["confidence"], row["evidence"]) == ("low", "공문 본문과 내용 유사")
