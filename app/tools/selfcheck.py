"""실행물 자체 점검 — 업무 복원에서 이어받기까지가 묶음 안에서 도는가.

    NunchiCoach.exe selftest <폴더>      (entry.py가 파서 점검 뒤에 부른다)
    python -m app.tools.selfcheck

파서 점검(parse_report)은 문서를 읽는 데까지만 본다. PyInstaller가 새 모듈이나
schema.sql의 새 표를 빠뜨리면 앱은 뜨지만 '지금 할 일'이나 복원본 저장에서만
조용히 깨진다. 임시 폴더에 작은 프로젝트를 만들어 그 길을 한 번 끝까지 간다.

    스키마 만들기 → 문서 읽기 → 조각·글자 검색 → 항목·절차·공백 복원 → 단계 수행 기록
    → 복원본 저장 → 묶음 내보내기 → 다른 프로젝트로 가져오기 → 원문 재연결

모델을 부르지 않고 네트워크도 쓰지 않는다. 임시 폴더는 끝나면 지운다.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

TEXT = ("절차: 집계 자료 준비 → 실적표 작성 → 검토 후 제출\n"
        "제출처: 기획예산과\n제출기한: 2025-09-30")


def run() -> list[str]:
    """실패한 점검의 설명 목록. 비어 있으면 모두 통과다."""
    from ..core import handover_release, reconstruction, transfer, workcard, workstate as ws
    from ..core.chunking import build_chunks
    from ..db import Database
    from ..ingest.parsers import parse
    from ..search import fts

    problems: list[str] = []
    root = Path(tempfile.mkdtemp(prefix="nunchi-selfcheck-"))
    opened = []
    try:

        def project(name: str):
            source = root / name / "자료"
            source.mkdir(parents=True)
            path = source / "2025_실적보고_요청.txt"
            path.write_text(TEXT, encoding="utf-8")
            db = Database(root / name / "project" / "project.db")
            db.init()
            opened.append(db)
            parsed = parse(path)
            doc = db.upsert_document(db.add_source(source), {
                "path": str(path), "filename": path.name, "ext": ".txt",
                "parse_status": parsed.status, "char_count": parsed.char_count,
                "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                "eff_date": "2025-09-01", "eff_year": 2025, "eff_month": 9,
                "eff_precision": "day", "eff_date_kind": "body"})
            db.replace_sections(doc, [(s.kind, s.ordinal, s.locator, s.text)
                                      for s in parsed.sections])
            db.index_document(doc, path.name, str(path), "", parsed.text)
            return db, doc

        sender, doc = project("보내는쪽")
        from ..core import notebook
        book_id = notebook.create(sender, "자체 점검 업무노트")
        notebook.add_sources(sender, book_id, [doc])
        if str(doc) not in notebook.selected_snapshot(sender, book_id):
            problems.append("업무노트에 원문 판본을 연결하지 못했다")
        # 질문 화면의 재료 — 조각과 조각 글자 검색은 모델 없이 생겨야 한다.
        sections = sender.con.execute(
            "SELECT kind, locator, text FROM document_sections WHERE doc_id = ? "
            "ORDER BY ordinal", (doc,)).fetchall()
        made = sender.replace_document_chunks(
            doc, build_chunks([(s["kind"], s["locator"], s["text"]) for s in sections]))
        if not made or made[0][0] not in fts.search_chunk_ids(sender, "기획예산과", limit=5):
            problems.append("문서를 조각으로 나눠 글자 검색에 올리지 못했다")
        task =sender.con.execute("INSERT INTO tasks(name) VALUES ('정기 실적보고')").lastrowid
        sender.assign_document(task, doc)
        reconstruction.restore(sender, date(2025, 9, 10))
        card = workcard.build(sender, task)
        if [s["label"] for s in card.stated_steps] != ["집계 자료 준비", "실적표 작성", "검토 후 제출"]:
            problems.append("문서에 적힌 절차를 단계로 세우지 못했다")
        if not any(i["content"] == "2025-09-30" for i in card.todo):
            problems.append("기한을 항목으로 올리지 못했다")
        if not card.actions or card.actions[0]["type"] != "do":
            problems.append("다음 행동을 내놓지 못했다")
        if card.stated_steps:
            sender.set_step_run(card.stated_steps[0]["id"], ws.DONE, note="자체 점검")
        from ..core.knowledge.lifecycle import run_jobs
        graph = run_jobs(sender, seconds=30)
        if graph.failed or not sender.con.execute('SELECT 1 FROM kg_current_claims LIMIT 1').fetchone():
            problems.append('그래프 영속 큐를 게시하지 못했다')
        from ..search.graph import search as graph_search
        from ..search.query import QueryScope
        found = graph_search(sender, '기획예산과', QueryScope(doc_ids=[doc]),
                             seed_chunks=[cid for cid, _ in made], budget_ms=2000)
        if not found.hits or found.fallback:
            problems.append('선택 원문의 그래프 근거를 검색하지 못했다')
        if graph_search(sender, '기획예산과', QueryScope(doc_ids=[])).hits:
            problems.append('빈 자료 선택 밖의 그래프 근거가 노출됐다')
        sender.con.execute("UPDATE documents SET missing_since=datetime('now') WHERE id=?", (doc,))
        if sender.con.execute("SELECT 1 FROM kg_current_claims WHERE predicate='due_date'").fetchone():
            problems.append('누락 원본의 기한이 현재 그래프에 남았다')
        sender.mark_seen(doc)
        run_jobs(sender, seconds=30)
        if not sender.con.execute("SELECT 1 FROM kg_current_claims WHERE predicate='due_date'").fetchone():
            problems.append('복귀 원본의 그래프 근거를 재검증하지 못했다')
        snapshot_id, created = handover_release.save(sender)
        if not created or "자료 기반 업무 복원본" not in handover_release.to_markdown(
                handover_release.load(sender, snapshot_id)):
            problems.append("복원본을 저장하거나 글로 옮기지 못했다")
        package = transfer.export_package(sender, snapshot_id, root / "묶음.zip")

        receiver, _doc = project("받는쪽")
        result = transfer.import_package(receiver, package)
        received = workcard.build(receiver, 1)
        if not received.stated_steps or received.stated_steps[0]["run_note"] != "자체 점검":
            problems.append("가져온 프로젝트에 수행 기록이 이어지지 않았다")
        if result["relink"][transfer.EXACT] == 0:
            problems.append("가져온 근거를 이 PC의 원문에 다시 잇지 못했다")
    except Exception as exc:   # 묶음에서 빠진 모듈·자원은 여기서 드러난다
        problems.append(f"{type(exc).__name__}: {exc}")
    finally:
        # 실패했을 때도 닫는다 — 열린 DB가 있으면 Windows에서 임시 폴더가 안 지워진다.
        for db in opened:
            db.close()
        shutil.rmtree(root, ignore_errors=True)
    return problems


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    problems = run()
    if problems:
        print("■ 업무 복원 자체 점검 — 실패")
        for problem in problems:
            print(f"  ✕ {problem}")
        return 1
    print("■ 업무 복원 자체 점검 — 통과 (복원 → 수행 기록 → 그래프 갱신·재개·범위 검색 → 복원본 → 묶음 → 가져오기 → 재연결)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
