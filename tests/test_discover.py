"""업무 발견 통합 시험.

계약 세 가지.
  1. 모델이 없어도 업무 목록이 나온다 (파일명 공통 낱말로 이름을 대신한다)
  2. 사용자가 고친 업무와 직접 배정한 문서를 재발견이 덮어쓰지 않는다
  3. 같은 이름이 나온 묶음은 하나로 합친다 — 업무 분류가 흩어지지 않게
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.ai.discover import discover
from app.db import Database
from app.search import pack


@pytest.fixture
def project(tmp_path: Path):
    db = Database(tmp_path / "p.db")
    db.init()
    yield db
    db.close()


def _seed(db: Database, groups: dict[str, int], dims: int = 6) -> dict[str, list[int]]:
    """업무별로 서로 먼 벡터 묶음을 심는다."""
    source_id = db.add_source(r"D:\자료")
    made: dict[str, list[int]] = {}
    for axis, (label, count) in enumerate(groups.items()):
        ids = []
        for index in range(count):
            name = f"202{index % 4 + 2}_{label}_자료{index}.hwp"
            doc_id = db.upsert_document(source_id, {
                "path": rf"D:\자료\{name}", "filename": name, "ext": ".hwp",
                "parse_status": "ok", "hash": f"{label}{index}",
                "char_count": 800, "eff_year": 2022 + index % 4,
                "eff_date": f"202{index % 4 + 2}-09-01", "eff_date_kind": "body",
            })
            vector = [0.02] * dims
            vector[axis] = 1.0
            db.save_doc_embedding(doc_id, "bge-m3", dims, pack(vector), 100)
            ids.append(doc_id)
        made[label] = ids
    return made


def test_discovery_works_without_a_model(project):
    """Ollama가 없어도 업무 목록이 나와야 한다."""
    _seed(project, {"행정사무감사": 5, "예산관리": 4})
    result = discover(project, client=None, min_size=3)

    assert result.clusters == 2
    assert result.tasks == 2
    assert result.named_by_model == 0
    assert result.named_by_filename == 2

    names = {row["name"] for row in project.tasks()}
    assert "행정사무감사" in names
    assert "예산관리" in names


def test_documents_are_attached_and_reading_list_is_built(project):
    seeded = _seed(project, {"행정사무감사": 5})
    discover(project, client=None, min_size=3)

    task = project.tasks()[0]
    assert task["doc_count"] == 5
    picks = project.task_reading(task["id"])
    assert picks, "먼저 읽을 문서를 고르지 못했습니다"
    assert all(pick["reason"] for pick in picks), "이유 없는 추천이 있습니다"
    assert {p["doc_id"] for p in picks} <= set(seeded["행정사무감사"])


def test_small_groups_stay_unclassified(project):
    _seed(project, {"행정사무감사": 5, "잡다": 2})
    result = discover(project, client=None, min_size=3)

    assert result.tasks == 1
    assert result.unassigned == 2
    assert project.unclassified_count() == 2


def test_rediscovery_is_idempotent(project):
    _seed(project, {"행정사무감사": 5, "예산관리": 4})
    discover(project, client=None, min_size=3)
    first = {(r["name"], r["doc_count"]) for r in project.tasks()}

    discover(project, client=None, min_size=3)
    assert {(r["name"], r["doc_count"]) for r in project.tasks()} == first


def test_user_renamed_task_survives_rediscovery(project):
    """사람이 고친 값은 재분석이 덮어쓰지 않는다 (NFR-SAF-004)."""
    _seed(project, {"행정사무감사": 5})
    discover(project, client=None, min_size=3)

    task_id = project.tasks()[0]["id"]
    assert project.rename_task(task_id, "의회 대응")

    discover(project, client=None, min_size=3)
    names = {row["name"] for row in project.tasks()}
    assert "의회 대응" in names

    row = project.task(task_id)
    assert row["status"] == "edited"
    assert row["origin"] == "user"


def test_manually_assigned_document_is_not_reassigned(project):
    seeded = _seed(project, {"행정사무감사": 5, "예산관리": 4})
    discover(project, client=None, min_size=3)

    # 사용자가 예산 문서 하나를 행감 업무로 직접 옮긴 상황
    audit_task = next(r["id"] for r in project.tasks() if "행정" in r["name"])
    moved = seeded["예산관리"][0]
    project.con.execute("DELETE FROM task_docs WHERE doc_id = ?", (moved,))
    project.con.execute(
        "INSERT INTO task_docs(task_id, doc_id, origin) VALUES (?, ?, 'user')",
        (audit_task, moved),
    )

    discover(project, client=None, min_size=3)
    rows = project.con.execute(
        "SELECT task_id, origin FROM task_docs WHERE doc_id = ?", (moved,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["task_id"] == audit_task
    assert rows[0]["origin"] == "user"


def test_rename_rejects_a_name_already_in_use(project):
    _seed(project, {"행정사무감사": 5, "예산관리": 4})
    discover(project, client=None, min_size=3)
    tasks = project.tasks()
    assert project.rename_task(tasks[0]["id"], tasks[1]["name"]) is False


def test_detaching_a_document_removes_it_from_the_reading_list(project):
    _seed(project, {"행정사무감사": 5})
    discover(project, client=None, min_size=3)
    task_id = project.tasks()[0]["id"]
    victim = project.task_reading(task_id)[0]["doc_id"]

    project.detach_document(task_id, victim)

    assert victim not in {r["doc_id"] for r in project.task_reading(task_id)}
    assert victim not in {r["id"] for r in project.task_documents(task_id)}
    corrections = project.con.execute("SELECT COUNT(*) AS n FROM corrections").fetchone()
    assert corrections["n"] >= 1, "교정 이력이 남지 않았습니다"


def test_discovery_without_embeddings_reports_why(project):
    project.add_source(r"D:\자료")
    result = discover(project, client=None)
    assert result.tasks == 0
    assert result.errors and "색인" in result.errors[0]


def test_duplicate_copies_are_not_recommended_twice(project):
    """추천 다섯 칸이 같은 문서로 채워지면 안 된다."""
    source_id = project.add_source(r"D:\중복자료")
    for index in range(4):
        name = f"2025_실적_사본{index}.xlsx"
        doc_id = project.upsert_document(source_id, {
            "path": rf"D:\중복자료\{name}", "filename": name, "ext": ".xlsx",
            "parse_status": "ok", "hash": "SAME", "char_count": 500,
            "eff_year": 2025, "eff_date": "2025-08-01", "eff_date_kind": "body",
        })
        project.save_doc_embedding(doc_id, "bge-m3", 3, pack([1.0, 0.01, 0.01]), 100)

    discover(project, client=None, min_size=3)
    picks = project.task_reading(project.tasks()[0]["id"])
    assert len(picks) == 1, [p["filename"] for p in picks]


# ── 벡터로 묶이지 않은 문서 ─────────────────────────────────────────
# 실제 모델로 묶었을 때 문서 수가 적고 형식이 섞인 업무 둘이 어느 업무에도
# 묶이지 않아 '지금 할 일' 카드가 생기지 않았다. 군집 기준은 그대로 두고,
# 남은 문서만 파일명으로 묶어 낮은 확신의 제안으로 올린다.

def _loose(db: Database, names: list[str], start: int = 0) -> list[int]:
    """서로 닮지 않은 벡터의 문서들 — 벡터로는 하나도 묶이지 않는다."""
    source_id = db.add_source(r"D:\자료")
    ids = []
    for index, name in enumerate(names, start=start):
        doc_id = db.upsert_document(source_id, {
            "path": rf"D:\자료\{name}", "filename": name, "ext": ".txt",
            "parse_status": "ok", "hash": f"loose{index}", "char_count": 300,
            "eff_year": 2025, "eff_date": "2025-05-01", "eff_date_kind": "body"})
        vector = [0.0] * 24
        vector[index] = 1.0
        db.save_doc_embedding(doc_id, "bge-m3", 24, pack(vector), 100)
        ids.append(doc_id)
    return ids


def test_leftover_documents_sharing_a_filename_topic_become_a_low_confidence_proposal(project):
    contract = _loose(project, ["2025_용역계약_체결보고.hwpx", "2025_용역_중간점검_회의.txt",
                                "2024_용역_정산_보고.txt"])
    _loose(project, ["2025_회의록.txt", "메모.txt"], start=3)
    result = discover(project, client=None, min_size=3)

    assert result.clusters == 0 and result.grouped_by_filename == 1
    assert result.unassigned == 2 and project.unclassified_count() == 2
    task = project.tasks()[0]
    assert task["name"] == "용역" and task["status"] == "proposed"
    assert task["confidence"] == "low"                 # 확신하는 것처럼 보이지 않는다
    assert "확인" in task["description"]
    assert {row["id"] for row in project.task_documents(task["id"])} == set(contract)
    assert project.task_reading(task["id"])

    # 다시 돌려도 불어나지 않는다.
    discover(project, client=None, min_size=3)
    assert [(row["name"], row["doc_count"]) for row in project.tasks()] == [("용역", 3)]


def test_leftovers_are_not_pushed_into_an_existing_task(project):
    """남은 문서의 파일명이 이미 있는 업무와 같아도 그 업무에 밀어 넣지 않는다."""
    seeded = _seed(project, {"예산관리": 4}, dims=24)
    _loose(project, ["2025_예산관리_메모1.txt", "2025_예산관리_메모2.txt",
                     "2025_예산관리_메모3.txt"], start=10)
    result = discover(project, client=None, min_size=3)

    assert result.grouped_by_filename == 0 and result.unassigned == 3
    task = project.tasks()[0]
    assert {row["id"] for row in project.task_documents(task["id"])} == set(seeded["예산관리"])


def test_two_leftover_documents_are_still_not_a_task(project):
    _loose(project, ["2025_보조금_지침.txt", "2024_보조금_통보.txt"])
    result = discover(project, client=None, min_size=3)
    assert result.tasks == 0 and result.unassigned == 2


# ── 재발견은 사람이 해 둔 일을 지우지 않는다 ────────────────────────
# 실제 모델로 전체 분석을 다시 돌리다 발견했다. 업무 재발견이 '아직 확인하지
# 않은 업무'를 지우고 새로 만들면서, 그 아래의 항목·단계·수행 기록까지 연쇄로
# 지웠다. 후임자가 '이 업무 확인함'을 누르지 않고 일부터 시작하는 것은 흔하다.

@pytest.mark.parametrize("work", ["step_run", "item_edit", "item_added", "step_added", "gap_closed"])
def test_rediscovery_keeps_an_unconfirmed_task_that_someone_worked_on(project, work):
    from app.core import workstate as ws

    _seed(project, {"행정사무감사": 5, "예산관리": 4})
    discover(project, client=None, min_size=3)
    touched = next(row["id"] for row in project.tasks() if row["name"] == "행정사무감사")
    other = next(row["id"] for row in project.tasks() if row["name"] == "예산관리")
    assert project.task(touched)["status"] == "proposed"      # 업무 자체는 확인하지 않았다

    for task_id in (touched, other):
        project.sync_task_items(task_id, [{"kind": "deadline", "slot": "제출기한",
                                           "content": "2025-09-30"}])
        project.sync_work_steps(task_id, [{"slot": "stated-1", "label": "취합", "ordinal": 1,
                                           "procedure_kind": ws.PROCEDURE_STATED}])
        project.sync_info_gaps(task_id, [{"gap_key": "missing:recipient", "kind": "missing",
                                          "field": "제출처"}])
    item = project.task_items(touched)[0]["id"]
    step = project.work_steps(touched)[0]["id"]
    if work == "step_run":
        project.set_step_run(step, ws.DONE, note="취합 끝")
    elif work == "item_edit":
        project.review_task_item(item, "edit", "2025-10-02")
    elif work == "item_added":
        project.add_task_item(touched, "note", "예산팀에 미리 연락")
    elif work == "step_added":
        project.add_work_step(touched, "과장 사전 보고")
    else:
        project.resolve_info_gap(project.info_gaps(touched)[0]["id"], "기획예산과로 확인")
    assert project.tasks_with_human_work() == {touched}

    discover(project, client=None, min_size=3)

    # 손댄 업무는 같은 번호로 남고, 그 아래의 것도 문서 배정도 그대로다.
    assert project.task(touched) is not None
    assert project.task_items(touched) and project.work_steps(touched)
    assert len(project.task_documents(touched)) == 5
    if work == "step_run":
        assert project.work_steps(touched)[0]["run_note"] == "취합 끝"
    if work == "item_edit":
        assert project.task_items(touched)[0]["content"] == "2025-10-02"
    # 아무도 손대지 않은 제안은 예전처럼 갈아 끼운다 — 새 행이라 아래가 비어 있고,
    # 다음 '업무 복원' 단계가 자료에서 다시 채운다.
    renewed = next(row["id"] for row in project.tasks() if row["name"] == "예산관리")
    assert project.task_items(renewed) == [] and project.work_steps(renewed) == []
    assert {row["name"] for row in project.tasks()} == {"행정사무감사", "예산관리"}
