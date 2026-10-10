"""2단계 도구 — 자료 찾기·비교·서식·체크리스트·검증 (행동형 업무 비서 계획서 T01~T12).

합성 시나리오(tests/fixtures/action_scenarios)를 실제 파일로 만들어 분석한 프로젝트에서
계획 틀을 돌린다. 수치 평가는 test_action_eval.py가 하고, 여기서는 하나씩의 약속을 본다.
"""

import io
import json
import os
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import openpyxl
import pytest
from PySide6.QtWidgets import QApplication

from app.actions import artifacts as ledger, catalog, context as ctx
from app.actions.registry import ToolError
from app.actions.runner import COMPLETED, FAILED, WAITING
from app.actions.tools import checks, form
from app.core import docrole, indicators, pii, yearcompare as yc
from app.tools import action_eval

TODAY = date(2026, 10, 10)
CORPUS = json.loads((action_eval.FIXTURES / "corpus.json").read_text(encoding="utf-8"))["scenarios"][0]


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def build(tmp_path):
    """분석이 끝난 프로젝트. (db, 업무 id, 경로 → 문서 id)."""
    source = tmp_path / "자료"
    action_eval.write_corpus(source, CORPUS["documents"])
    return action_eval.analyse(tmp_path / "project", source, CORPUS, TODAY, CORPUS["documents"])


@pytest.fixture
def project(tmp_path):
    db, task_id, by_path = build(tmp_path)
    yield db, task_id, by_path
    db.close()


def outputs(db, run_id):
    return {step["step_key"]: json.loads(step["output"]) for step in db.run_steps(run_id)}


def start(db, task_id):
    runner, run_id = catalog.prepare(db, task_id, 2026, today=TODAY)
    return runner, run_id


# ── 자료 찾기 (T01·T02·T10) ─────────────────────────────────────────

def test_discovery_separates_candidates_suspects_and_other_tasks(project):
    db, task_id, by_path = project
    runner, run_id = start(db, task_id)
    assert runner.execute(run_id) == WAITING
    found = outputs(db, run_id)["discover"]
    ids = {path: doc_id for path, doc_id in by_path.items()}
    included = {i["id"] for i in found["included"]}
    assert ids["2025/2025_규제혁신평가_평가지표.hwpx"] in included
    assert [c["id"] for c in found["candidates"]] == [ids["2024/2024_규제혁신평가_증빙자료_목록.hwpx"]]
    assert [s["id"] for s in found["suspect"]] == [ids["2025/2025_정부합동평가_실적_제출.hwpx"]]
    assert ids["2025/2025_정부합동평가_실적_제출.hwpx"] not in included
    assert ids["2024/2024_정부합동평가_지표.hwpx"] not in included | {c["id"] for c in found["candidates"]}


def test_candidates_and_suspects_never_feed_values_into_the_comparison(project):
    db, task_id, by_path = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    comparison = db.artifacts(run_id=run_id)[0]
    cited = {e["doc_id"] for e in db.evidence_for("artifact", [comparison["id"]]).get(comparison["id"], [])}
    assert by_path["2024/2024_규제혁신평가_증빙자료_목록.hwpx"] not in cited
    assert by_path["2025/2025_정부합동평가_실적_제출.hwpx"] not in cited
    assert cited and all(e["current"] for e in db.evidence_for("artifact", [comparison["id"]])[comparison["id"]])


def test_unreadable_documents_are_reported_with_a_reason(project):
    db, task_id, by_path = project
    path = "2025/2025_규제혁신평가_제출본.hwpx"
    db.con.execute("UPDATE documents SET parse_status = 'failed' WHERE id = ?", (by_path[path],))
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    found = outputs(db, run_id)["discover"]
    assert found["unreadable"] == [{"id": by_path[path], "why": "읽지 못함"}]
    assert by_path[path] not in {i["id"] for i in found["included"]}


def test_nothing_readable_stops_and_says_what_to_do(project):
    """T10. 읽을 수 있는 문서가 없으면 우회하지 않고 조치를 안내하며 멈춘다."""
    db, task_id, _ = project
    db.con.execute("UPDATE documents SET parse_status = 'failed'")
    runner, run_id = start(db, task_id)
    assert runner.execute(run_id) == FAILED
    step = db.run_steps(run_id)[0]
    assert step["state"] == "blocked" and "권한" in step["error"] and step["attempts"] == 0
    assert db.artifacts(run_id=run_id) == []


# ── 비교 (T03·T04·T05) ──────────────────────────────────────────────

def indicator(name, score="30", definition="", formula="", proof="", doc=1, locator="표"):
    return indicators.Indicator(name, score, definition, formula, proof, doc, locator, f"{name} {score}")


def keyed(*rows):
    return {row.key(): row for row in rows}


def test_same_name_with_a_different_cell_is_changed_and_a_blank_is_not_same():
    left = keyed(indicator("규제 발굴", "30", formula="건수 × 3"), indicator("이행", "40"))
    right = keyed(indicator("규제 발굴", "30", formula="건수 × 3"), indicator("이행", "40", formula="완료 ÷ 대상"))
    changes = {c.name: c for c in yc.compare_indicators(left, right)}
    assert changes["규제 발굴"].status == yc.SAME
    assert changes["이행"].status == yc.CHANGED
    assert changes["이행"].fields == [(indicators.FORMULA, yc.BLANK, "완료 ÷ 대상")]


def test_similar_names_are_not_called_added_or_removed():
    left = keyed(indicator("기업 간담회 운영", "30"))
    right = keyed(indicator("기업 간담회 운영 실적", "30"))
    [change] = yc.compare_indicators(left, right)
    assert change.status == yc.UNCERTAIN and "같은 지표인지" in change.note and len(change.refs) == 2


def test_a_year_without_a_readable_table_makes_the_area_insufficient(project):
    db, task_id, by_path = project
    docrole.set_role(db, by_path["2024/2024_규제혁신평가_평가지표.hwpx"], docrole.OTHER)
    ids = [row["doc_id"] for row in db.con.execute("SELECT doc_id FROM task_docs WHERE task_id = ?", (task_id,))]
    pair = yc.build(db, ids, 2026).pairs[0]
    assert pair.indicators_state == yc.INSUFFICIENT and pair.indicators == []
    assert "2024년" in pair.indicators_note


def test_target_year_cells_stay_empty_without_official_documents(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    steps = outputs(db, run_id)
    assert steps["classify"]["target"]["basis"] == "unconfirmed"
    assert steps["compare"]["mode"] == steps["form"]["mode"] == "preliminary"
    report = next(db.artifact(i) for i in steps["compare"]["artifacts"].values()
                  if db.artifact(i)["kind"] == "comparison")
    text = Path(report["path"]).read_text(encoding="utf-8")
    assert "2026년은 올해 공식 문서가 없어 견주지 않았습니다" in text and "→ 2026년" not in text
    assert "과거 값" in text


def test_revision_group_without_a_clear_latest_is_flagged(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    questions = next(r for r in db.artifacts(run_id=run_id) if r["kind"] == "open_questions")
    assert "최신본을 정하지 못했습니다" in Path(questions["path"]).read_text(encoding="utf-8")


# ── 서식 (T03) ──────────────────────────────────────────────────────

def test_form_keeps_last_years_columns_and_marks_itself_preliminary(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    steps = outputs(db, run_id)
    workbook = openpyxl.load_workbook(db.artifact(steps["form"]["artifacts"]["form"])["path"])
    assert workbook.sheetnames == list(form.SHEETS)
    assert [c.value for c in workbook["03_부서별실적"][1]][1:] == [
        "부서", "구분", "추진내용", "추진일", "증빙자료", "부서장 확인", "검증상태", "점검"]
    banner = workbook["01_작성안내"]["A3"].value
    assert form.NOT_OFFICIAL in banner and "확인 필요" in banner
    assert {dv.type for dv in workbook["03_부서별실적"].data_validations.dataValidation} == {"date", "list"}
    check = json.loads(Path(db.artifact(steps["form"]["artifacts"]["check"])["path"]).read_text(encoding="utf-8"))
    assert all(c["passed"] for c in check["checks"]) and "계산" in check["limits"][0]


def test_form_example_rows_from_last_year_are_not_copied(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    workbook = openpyxl.load_workbook(db.artifact(outputs(db, run_id)["form"]["artifacts"]["form"])["path"])
    sheet = workbook["03_부서별실적"]
    assert all(sheet.cell(row=r, column=c).value is None for r in range(2, 6) for c in range(2, 8))


def test_workbook_check_catches_structure_problems():
    workbook = openpyxl.Workbook()
    workbook.active.title = "엉뚱한 시트"
    buffer = io.BytesIO()
    workbook.save(buffer)
    assert [c["passed"] for c in form.check_workbook(buffer.getvalue(), "preliminary", [], 0)] == [False]

    broken = openpyxl.Workbook()
    broken.active.title = form.SHEETS[0]
    for name in form.SHEETS[1:]:
        broken.create_sheet(name)
    broken[form.SHEETS[4]]["A1"] = "=COUNTIF('없는시트'!A1:A5,1)"
    buffer = io.BytesIO()
    broken.save(buffer)
    failed = {c["name"] for c in form.check_workbook(buffer.getvalue(), "preliminary", [], 0) if not c["passed"]}
    assert {"필수 열", "수식이 가리키는 시트"} <= failed


def test_column_roles_read_the_header_names():
    assert form.column_roles(["부서", "구분", "추진내용", "추진일", "증빙자료"]) == {
        "dept": 0, "type": 1, "content": 2, "date": 3, "proof": 4}
    assert "dept" not in form.column_roles(["구분", "추진내용"])


# ── 체크리스트와 승인 ───────────────────────────────────────────────

def test_checklist_marks_where_each_item_comes_from_and_has_no_dates(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    steps = outputs(db, run_id)["checklist"]
    items = json.loads(Path(db.artifact(steps["artifact_id"])["path"]).read_text(encoding="utf-8"))["items"]
    assert steps["with_due_date"] == 0 and steps["sources"]["official"] == 0
    assert items[0]["title"].startswith("2026년 공식 평가계획") and items[0]["source_type"] == "suggested"
    stated = [i for i in items if i["source_type"] == "stated"]
    assert [i["title"] for i in stated] == ["부서별 실적 취합", "부서별 확인", "증빙자료 확인", "평가자료 작성", "기획조정실 제출"]
    assert all(i.get("work_step_id") for i in stated)
    assert any(i["source_type"] == "precedent" and "과거 지적사항" in i["description"] for i in items)
    assert {i["code"] for i in items} == {f"C-{n:02d}" for n in range(1, len(items) + 1)}


def test_approved_checklist_is_registered_and_human_edits_survive(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    assert runner.execute(run_id) == WAITING
    pending = runner.pending_approval(run_id)
    assert pending["tool"] == "register_checklist" and pending["scope"]["sha256"]
    assert db.checklist(db.run(run_id)["case_id"]) == []        # 승인 전에는 올리지 않았다

    runner.decide(pending["step_id"], True)
    assert runner.execute(run_id) == COMPLETED
    case_id = db.run(run_id)["case_id"]
    rows = db.checklist(case_id)
    assert len(rows) == outputs(db, run_id)["checklist"]["items"]
    assert db.artifact(outputs(db, run_id)["checklist"]["artifact_id"])["state"] == "reviewed"
    chain = {row["slot"]: row for row in rows}
    assert chain["base:diff"]["depends_on"] == chain["base:plan"]["id"]

    db.set_check_state(chain["base:plan"]["id"], "done")
    mine = db.add_check_item(case_id, "법무팀 협조 요청")
    again = db.sync_checklist(case_id, run_id, json.loads(Path(db.artifact(
        outputs(db, run_id)["checklist"]["artifact_id"])["path"]).read_text(encoding="utf-8"))["items"])
    assert again["added"] == 0 and again["removed"] == 0
    assert db.con.execute("SELECT state FROM checklist_items WHERE id = ?",
                          (chain["base:plan"]["id"],)).fetchone()[0] == "done"
    assert db.con.execute("SELECT origin FROM checklist_items WHERE id = ?", (mine,)).fetchone()[0] == "user"


def test_checklist_state_change_follows_the_linked_step(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    runner.decide(runner.pending_approval(run_id)["step_id"], True)
    runner.execute(run_id)
    row = next(r for r in db.checklist(db.run(run_id)["case_id"]) if r["work_step_id"])
    db.set_check_state(row["id"], "done", note="부서 회신 확인")
    run = db.con.execute("SELECT state, note FROM step_runs WHERE step_id = ?", (row["work_step_id"],)).fetchone()
    assert (run["state"], run["note"]) == ("done", "부서 회신 확인")


def test_registered_checklist_that_changed_after_approval_is_refused(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    runner.decide(runner.pending_approval(run_id)["step_id"], True)
    row = db.artifact(outputs(db, run_id)["checklist"]["artifact_id"])
    Path(row["path"]).write_text('{"items": []}', encoding="utf-8")
    assert runner.execute(run_id) == "partial"           # 앞 단계는 끝났고 올리기만 실패했다
    assert "바뀌었거나 없어졌습니다" in db.run_steps(run_id)[-1]["error"]
    assert db.checklist(db.run(run_id)["case_id"]) == []


def test_rerun_with_the_same_materials_is_the_same_run(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    count = lambda t: db.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    before = (count("action_runs"), count("artifacts"), count("work_cases"))
    again, same = start(db, task_id)
    assert same == run_id and again.execute(same) == WAITING
    assert (count("action_runs"), count("artifacts"), count("work_cases")) == before


# ── 검증 (V1) ───────────────────────────────────────────────────────

def verify_context(db, run_id):
    reader = ctx.ReadOnlyDatabase(db.path)
    run = db.run(run_id)
    return reader, ctx.build(db, reader, "draft", run_id=run_id, step_id=db.run_steps(run_id)[5]["id"],
                             task_id=run["task_id"], case_id=run["case_id"], today=TODAY,
                             should_stop=lambda: False)


def test_verification_notices_a_file_changed_after_it_was_made(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    steps = outputs(db, run_id)
    Path(db.artifact(steps["compare"]["artifacts"]["comparison"])["path"]).write_text("바꿈", encoding="utf-8")
    reader, context = verify_context(db, run_id)
    try:
        with pytest.raises(ToolError, match="만든 뒤 바뀌었거나"):
            checks.run_verify(context, {"inputs": steps})
    finally:
        reader.close()


def test_verification_fails_when_the_preliminary_mark_is_missing(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    steps = outputs(db, run_id)
    guide = db.artifact(steps["form"]["artifacts"]["guide"])
    stripped = Path(guide["path"]).read_text(encoding="utf-8").replace(form.NOT_OFFICIAL, "")
    Path(guide["path"]).write_bytes(stripped.encode("utf-8"))   # 줄바꿈이 바뀌면 지문이 달라진다
    db.con.execute("UPDATE artifacts SET sha256 = ? WHERE id = ?",
                   (__import__("hashlib").sha256(stripped.encode("utf-8")).hexdigest(), guide["id"]))
    reader, context = verify_context(db, run_id)
    try:
        with pytest.raises(ToolError, match="미확인 표기가 없습니다"):
            checks.run_verify(context, {"inputs": steps})
    finally:
        reader.close()


def test_dates_in_the_target_year_need_a_reason():
    text = "제출 2026-11-14, 만든 날 2026-10-10, 2026. 3. 5. 도착, 2026년 평가, 2025-11-14"
    assert checks.unfounded_dates(text, 2026, {"2026-10-10"}) == ["2026-11-14", "2026-03-05"]


def test_personal_data_is_counted_but_never_returned():
    text = "문의 051-123-4567, 담당 kim@example.go.kr, 주민번호 900101-1234567"
    found = pii.scan(text)
    assert found == {"resident": 1, "phone": 1, "email": 1}
    assert "051" not in pii.describe(found) and "kim" not in pii.describe(found)
    assert pii.scan("2025. 11. 14. 제출 12,345원 100-200") == {}


def test_verification_reports_personal_data_without_failing(project):
    db, task_id, _ = project
    runner, run_id = start(db, task_id)
    runner.execute(run_id)
    steps = outputs(db, run_id)
    memo = db.artifact(steps["compare"]["artifacts"]["open_questions"])
    text = Path(memo["path"]).read_text(encoding="utf-8") + "\n문의 051-123-4567\n"
    Path(memo["path"]).write_bytes(text.encode("utf-8"))
    db.con.execute("UPDATE artifacts SET sha256 = ? WHERE id = ?",
                   (__import__("hashlib").sha256(text.encode("utf-8")).hexdigest(), memo["id"]))
    reader, context = verify_context(db, run_id)
    try:
        result = checks.run_verify(context, {"inputs": steps})
    finally:
        reader.close()
    assert result["failures"] == 0 and result["pii"] == 1 and result["warnings"] >= 1
    report = Path(db.artifact(result["artifact_id"])["path"]).read_text(encoding="utf-8")
    assert "전화번호 1건" in report and "051" not in report


# ── 문서 속 지시문 (T11) ────────────────────────────────────────────

def test_instruction_inside_a_document_changes_nothing(tmp_path):
    """같은 자료에서 참고메모의 지시문만 빼도 계획·도구·산출물 수가 같다."""
    results = []
    for index, with_memo in enumerate((True, False)):
        folder = tmp_path / str(index)
        folder.mkdir()
        documents = [d for d in CORPUS["documents"] if with_memo or not d["path"].endswith("참고메모.txt")]
        source = folder / "자료"
        action_eval.write_corpus(source, documents)
        db, task_id, _ = action_eval.analyse(folder / "project", source, CORPUS, TODAY, documents)
        try:
            runner, run_id = start(db, task_id)
            runner.execute(run_id)
            texts = " ".join(ledger_text for ledger_text in
                             (checks.artifact_text(r) for r in db.artifacts(run_id=run_id)))
            results.append(([s["tool"] for s in db.run_steps(run_id)], len(db.artifacts(run_id=run_id)), texts))
        finally:
            db.close()
    assert results[0][:2] == results[1][:2]
    assert "발송하라" not in results[0][2] and "2026. 10. 20" not in results[0][2]
    assert not [n for n in catalog.REGISTRY.names() if "send" in n or "submit" in n]
