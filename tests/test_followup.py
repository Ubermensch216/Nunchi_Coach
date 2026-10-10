"""후속 실행 — 요청문 초안·서식 확정·회신 취합·증빙 대조·보고서 초안 (행동형 업무 비서 계획서 4단계).

회신 폴더는 tests/fixtures/action_scenarios/replies.json으로 합성한다(전부 지어낸 자료).
"""

import json
import os
import zipfile
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import openpyxl
import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from app.actions import catalog, replies
from app.actions.runner import COMPLETED, FAILED, PARTIAL, WAITING
from app.actions.tools import checks
from app.core import cases
from app.proactive import briefing, recommend
from app.tools import action_eval, action_judge
from app.ui import theme
from app.ui.views import workbox

TODAY = date(2026, 10, 10)
CORPUS = json.loads((action_eval.FIXTURES / "corpus.json").read_text(encoding="utf-8"))["scenarios"][0]
SPEC = json.loads((action_eval.FIXTURES / "replies.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    return app


def build(tmp_path, prepared=True):
    """분석한 프로젝트. prepared면 준비 작업을 끝까지(승인 포함) 돌려 둔다. (db, 업무 id, 수행 건 id)."""
    source = tmp_path / "자료"
    action_eval.write_corpus(source, CORPUS["documents"])
    db, task_id, _ = action_eval.analyse(tmp_path / "project", source, CORPUS, TODAY, CORPUS["documents"])
    if not prepared:
        return db, task_id, None
    runner, run_id = catalog.prepare(db, task_id, 2026, today=TODAY)
    runner.execute(run_id)
    runner.decide(runner.pending_approval(run_id)["step_id"], True)
    assert runner.execute(run_id) == COMPLETED
    return db, task_id, db.run(run_id)["case_id"]


@pytest.fixture
def project(tmp_path):
    db, task_id, case_id = build(tmp_path)
    yield db, task_id, case_id
    db.close()


def outputs(db, run_id):
    return {step["step_key"]: json.loads(step["output"]) for step in db.run_steps(run_id)}


def reply_folder(tmp_path):
    folder = tmp_path / "회신"
    action_judge.write_replies(folder, SPEC)
    return folder


# ── 회신 읽기 (순수한 부분) ─────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (datetime(2026, 3, 10), "2026-03-10"), ("2026. 6. 3.", "2026-06-03"), ("2026-12-31", "2026-12-31"),
    ("2026/7/4 완료", "2026-07-04"), (45000, "2023-03-15"), ("내년 3월", ""), ("2026-13-45", ""), (None, ""),
])
def test_dates_are_read_the_way_people_write_them(value, expected):
    assert replies.parse_date(value)[0] == expected


def test_bad_text_keeps_the_original_wording():
    assert replies.parse_date("내년 3월") == ("", "내년 3월")


def test_each_problem_file_is_reported_by_name_and_does_not_stop_the_others(tmp_path):
    folder = reply_folder(tmp_path)
    files, rows = replies.read_folder(folder, TODAY, {"규제 발굴 건수", "개선 과제 이행", "적극 해석 사례"})
    by_name = {f.name: f for f in files}
    assert "BadZipFile" in by_name["회신/깨진파일.xlsx"].problems[0]
    assert "실적표 시트를 찾지 못했습니다" in by_name["회신/시트없음.xlsx"].problems[0]
    assert len(rows) == 9 and {r.dept for r in rows} == {"교통과", "환경과", "경제과"}
    assert not any("작성 예" in r.content or "예시" in r.content for r in rows)          # 예시 줄은 실적이 아니다


def test_limits_protect_against_huge_inputs(tmp_path, monkeypatch):
    folder = reply_folder(tmp_path)
    monkeypatch.setattr(replies, "MAX_BYTES", 10)
    files, rows = replies.read_folder(folder, TODAY)
    assert rows == [] and all("너무 큽니다" in f.problems[0] for f in files)
    monkeypatch.setattr(replies, "MAX_BYTES", 15 * 1024 * 1024)
    monkeypatch.setattr(replies, "MAX_UNCOMPRESSED", 100)
    files, rows = replies.read_folder(folder, TODAY)
    assert rows == [] and any("압축을 풀면 너무 커서" in p for f in files for p in f.problems)
    monkeypatch.setattr(replies, "MAX_UNCOMPRESSED", 120 * 1024 * 1024)
    monkeypatch.setattr(replies, "MAX_ROWS", 0)
    monkeypatch.setattr(replies, "HEADER_SCAN", 2)
    files, rows = replies.read_folder(folder, TODAY)
    assert any("줄까지만 읽었습니다" in p for f in files for p in f.problems)


def test_temp_and_hidden_files_are_not_replies(tmp_path):
    folder = reply_folder(tmp_path)
    (folder / "회신" / "~$교통과_실적.xlsx").write_bytes(b"lock")
    (folder / "회신" / ".숨김.xlsx").write_bytes(b"x")
    assert all(not p.name.startswith(("~$", ".")) for p in replies.list_files(folder))


def test_department_falls_back_to_the_file_name(tmp_path):
    book = openpyxl.Workbook()
    book.active.title = "03_부서별실적"
    book.active.append(["구분", "추진내용", "추진일", "증빙자료"])
    book.active.append(["규제 발굴 건수", "신고 절차 정비 사례", datetime(2026, 4, 1), "목록"])
    book.save(tmp_path / "기획예산과.xlsx")
    _, rows = replies.read_folder(tmp_path, TODAY)
    assert [r.dept for r in rows] == ["기획예산과"] and rows[0].issues == []


def test_duplicates_are_marked_never_removed(tmp_path):
    files, rows = replies.read_folder(reply_folder(tmp_path), TODAY)
    grouped = [r for r in rows if r.group]
    assert len(rows) == 9 and len(grouped) == 4 and {r.group for r in grouped} == {1, 2}
    assert {r.dept for r in grouped if r.group == 1} == {"환경과", "경제과"}
    assert all(replies.DUPLICATE in r.issues for r in grouped)


def test_near_duplicates_are_found_inside_the_same_kind():
    rows = [replies.Row("a.xlsx", 2, "가과", "개선", "재활용 선별장 운영 기준을 개정함", "2026-01-02", "", "목록"),
            replies.Row("b.xlsx", 2, "나과", "개선", "재활용 선별장 운영 기준을 전면 개정함", "2026-01-05", "", "목록"),
            replies.Row("c.xlsx", 2, "다과", "개선", "공영주차장 요금 체계 개편 추진", "2026-01-09", "", "목록")]
    assert replies.mark_duplicates(rows) == 1 and [r.group for r in rows] == [1, 1, None]


def test_personal_data_is_flagged_without_its_value(tmp_path):
    _, rows = replies.read_folder(reply_folder(tmp_path), TODAY)
    flagged = [r for r in rows if replies.PERSONAL in r.issues]
    assert len(flagged) == 1 and flagged[0].personal == {"phone": 1}


def test_evidence_statuses_are_never_a_verdict(tmp_path):
    folder = reply_folder(tmp_path)
    files, rows = replies.read_folder(folder, TODAY)
    candidates = replies.proof_files(folder, {f.name for f in files})
    assert {path.name for path, _ in candidates} == {"발굴 과제 목록.txt", "시행 공문.txt", "간담회 회의록.txt"}
    cache = {}
    for row in rows:
        replies.match_row(row, candidates, cache)
    assert {r.evidence for r in rows} == {"ok", "none", "weak", "missing"}
    assert all(label for label in replies.EVIDENCE_LABEL.values())
    assert not any("인정" in label or "승인" in label for label in replies.EVIDENCE_LABEL.values())


# ── 요청문 초안과 서식 확정 ─────────────────────────────────────────

def test_request_needs_a_prepared_form(tmp_path):
    db, task_id, _ = build(tmp_path, prepared=False)
    try:
        case_id = cases.open_case(db, task_id, 2026)
        runner, run_id = catalog.prepare_request(db, case_id, today=TODAY)
        assert runner.execute(run_id) == FAILED
        step = db.run_steps(run_id)[0]
        assert step["state"] == "blocked" and "준비 작업" in step["error"] and db.artifacts(run_id=run_id) == []
    finally:
        db.close()


def test_stale_form_is_not_offered_for_distribution(project):
    db, _, case_id = project
    db.mark_artifacts_stale(case_id, ("preliminary",))
    runner, run_id = catalog.prepare_request(db, case_id, today=TODAY)
    assert runner.execute(run_id) == FAILED and "낡았습니다" in db.run_steps(run_id)[0]["error"]


def test_request_draft_fills_only_what_is_known_and_never_sends(project):
    db, _, case_id = project
    runner, run_id = catalog.prepare_request(db, case_id, today=TODAY)
    assert runner.execute(run_id) == WAITING
    draft = next(r for r in db.artifacts(run_id=run_id) if r["kind"] == "request_draft")
    text = Path(draft["path"]).read_text(encoding="utf-8")
    assert "사전조사용" in text and "발송하지 않습니다" in text
    assert "제출 기한: (확인 필요)" in text and "회신 받을 곳: (담당 부서·담당자·연락처 (확인 필요))" in text
    assert "붙임 서식 ‘부서별 실적취합 서식’" in text
    assert "지난해 시행문의 문구 (참고" in text and "시행계획 알림" in text          # 출처가 붙은 참고 절
    assert checks.unfounded_dates(text, 2026, {TODAY.isoformat()}) == []
    assert outputs(db, run_id)["request_draft"]["due_filled"] is False
    with zipfile.ZipFile(next(r for r in db.artifacts(run_id=run_id) if r["kind"] == "request_draft_hwpx")["path"]) as z:
        assert "Contents/section0.xml" in z.namelist()


def test_request_uses_a_confirmed_due_date_and_says_where_it_came_from(project):
    db, _, case_id = project
    cases.set_due(db, case_id, "2026-11-13", cases.OFFICIAL)
    runner, run_id = catalog.prepare_request(db, case_id, today=TODAY)
    runner.execute(run_id)
    text = Path(next(r for r in db.artifacts(run_id=run_id) if r["kind"] == "request_draft")["path"]).read_text(encoding="utf-8")
    assert "제출 기한: 2026-11-13 (공식 기한)" in text


def test_finalizing_the_form_waits_for_approval_and_can_be_declined(project):
    db, _, case_id = project
    form = next(a for a in db.artifacts(case_id=case_id) if a["kind"] == "collection_form")
    runner, run_id = catalog.prepare_request(db, case_id, today=TODAY)
    runner.execute(run_id)
    pending = runner.pending_approval(run_id)
    assert pending["tool"] == "finalize_form" and pending["scope"]["sha256"] == form["sha256"]
    assert db.artifact(form["id"])["state"] != "final"
    runner.decide(pending["step_id"], False)
    assert runner.execute(run_id) == COMPLETED and db.artifact(form["id"])["state"] != "final"


def test_approval_binds_to_the_form_that_was_shown(project):
    db, _, case_id = project
    form = next(a for a in db.artifacts(case_id=case_id) if a["kind"] == "collection_form")
    runner, run_id = catalog.prepare_request(db, case_id, today=TODAY)
    runner.execute(run_id)
    runner.decide(runner.pending_approval(run_id)["step_id"], True)
    Path(form["path"]).write_bytes(b"PK-changed")
    assert runner.execute(run_id) == PARTIAL
    assert "바뀌었거나 없어졌습니다" in db.run_steps(run_id)[-1]["error"] and db.artifact(form["id"])["state"] != "final"


def test_request_verification_stops_a_draft_with_an_unfounded_date(project, monkeypatch):
    from app.actions.tools import request

    original = request.render_request
    monkeypatch.setattr(request, "render_request", lambda *a: (original(*a)[0] + "\n제출 기한: 2026-11-14", []))
    db, _, case_id = project
    runner, run_id = catalog.prepare_request(db, case_id, today=TODAY)
    assert runner.execute(run_id) == PARTIAL
    assert "올해 공식 근거가 없는데 2026년 날짜" in db.run_steps(run_id)[1]["error"]
    assert db.run_steps(run_id)[2]["state"] == "pending"                          # 확정 단계는 승인조차 묻지 않았다


# ── 회신 취합 ───────────────────────────────────────────────────────

def test_intake_needs_a_folder_with_replies(project, tmp_path):
    db, _, case_id = project
    runner, run_id = catalog.prepare_intake(db, case_id, tmp_path / "없는폴더", today=TODAY)
    assert runner.execute(run_id) == FAILED and "회신 폴더를 찾을 수 없습니다" in db.run_steps(run_id)[0]["error"]
    empty = tmp_path / "빈폴더"
    empty.mkdir()
    runner, run_id = catalog.prepare_intake(db, case_id, empty, today=TODAY)
    assert runner.execute(run_id) == FAILED and "회신 엑셀 파일(.xlsx)이 없습니다" in db.run_steps(run_id)[0]["error"]


def test_intake_makes_a_new_book_and_a_report_without_touching_the_replies(project, tmp_path):
    db, _, case_id = project
    folder = reply_folder(tmp_path)
    before = action_judge._fingerprint(folder)
    runner, run_id = catalog.prepare_intake(db, case_id, folder, today=TODAY)
    assert runner.execute(run_id) == COMPLETED
    assert action_judge._fingerprint(folder) == before
    made = {r["kind"]: r for r in db.artifacts(run_id=run_id)}
    assert set(made) == {"collection_data", "evidence_check", "collection_book", "report_draft",
                         "report_draft_hwpx", "run_check"}
    book = openpyxl.load_workbook(made["collection_book"]["path"])
    assert book.sheetnames == ["안내", "취합", "부서 현황", "점검 목록", "중복 의심", "파일 점검"]
    rows = list(book["취합"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 9 and {r[11] for r in rows} == {"미검증"}
    assert sum(1 for r in rows if r[8]) == 4                                           # 중복 의심도 두 줄 모두 남았다
    assert any("불가" in str(r[7]) or "필수값 누락" in str(r[7]) for r in rows)
    report = Path(made["report_draft"]["path"]).read_text(encoding="utf-8")
    assert "사전조사용" in report and "| 교통과 | 2 | 1 |" in report and "중복 의심 2묶음" in report
    assert not any(word in report for word in ("인정됨", "승인됨", "검증 완료"))


def test_rerunning_with_the_same_replies_is_the_same_run_and_new_replies_make_a_new_version(project, tmp_path):
    db, _, case_id = project
    folder = reply_folder(tmp_path)
    runner, run_id = catalog.prepare_intake(db, case_id, folder, today=TODAY)
    runner.execute(run_id)
    first_book = next(a for a in db.artifacts(run_id=run_id) if a["kind"] == "collection_book")
    again, same = catalog.prepare_intake(db, case_id, folder, today=TODAY)
    assert same == run_id

    late = openpyxl.Workbook()
    late.active.title = "03_부서별실적"
    late.active.append(["부서", "구분", "추진내용", "추진일", "증빙자료"])
    late.active.append(["복지과", "적극 해석 사례", "돌봄 서비스 신청 요건 적극 해석", datetime(2026, 8, 3), "시행 공문"])
    late.save(folder / "회신" / "복지과_실적.xlsx")
    runner, newer = catalog.prepare_intake(db, case_id, folder, today=TODAY)
    assert newer != run_id and runner.execute(newer) == COMPLETED
    assert db.artifact(first_book["id"])["state"] == "superseded"
    new_book = next(a for a in db.artifacts(run_id=newer) if a["kind"] == "collection_book")
    assert new_book["version"] == first_book["version"] + 1
    assert openpyxl.load_workbook(new_book["path"])["취합"].max_row - 1 == 10


def test_intake_summary_holds_counts_not_text(project, tmp_path):
    db, _, case_id = project
    runner, run_id = catalog.prepare_intake(db, case_id, reply_folder(tmp_path), today=TODAY)
    runner.execute(run_id)
    saved = json.dumps(outputs(db, run_id), ensure_ascii=False)
    assert "공영주차장" not in saved and "051-123" not in saved and "교통과" not in saved


def test_verification_reports_personal_data_in_the_book_as_a_warning(project, tmp_path):
    db, _, case_id = project
    runner, run_id = catalog.prepare_intake(db, case_id, reply_folder(tmp_path), today=TODAY)
    assert runner.execute(run_id) == COMPLETED
    check = next(a for a in db.artifacts(run_id=run_id) if a["kind"] == "run_check")
    text = Path(check["path"]).read_text(encoding="utf-8")
    assert "전화번호" in text and "051" not in text and "회신 취합 검증 결과" in text


# ── 다음 단계 제안 ──────────────────────────────────────────────────

def test_a_finished_preparation_suggests_the_request_draft_once(project):
    db, task_id, case_id = project
    summary = recommend.scan(db, TODAY)
    assert summary["next"] == 1
    [rec] = [r for r in db.recommendations(("new",)) if r["plan_key"] == "collection_request"]
    assert rec["certainty"] == "suggested" and rec["rec_key"].endswith(f":{case_id}")
    card = next(c for c in briefing.build(db, TODAY).cards if c.rec_id == rec["id"])
    assert card.kind == "collection_request" and "발송은 이 프로그램이 하지 않습니다" in " ".join(card.why)
    assert any("승인한 뒤에만" in line for line in card.actions) and any("새 초안 만들기" in line for line in card.actions)
    assert recommend.scan(db, TODAY)["next"] == 0


def test_accepting_the_suggestion_starts_the_request_run(project):
    db, _, case_id = project
    recommend.scan(db, TODAY)
    rec = next(r for r in db.recommendations(("new",)) if r["plan_key"] == "collection_request")
    runner, run_id = recommend.accept(db, rec["id"], TODAY)
    assert db.run(run_id)["plan_key"] == "collection_request" and db.run(run_id)["recommendation_id"] == rec["id"]
    assert db.recommendation(rec["id"])["state"] == "accepted" and db.run(run_id)["case_id"] == case_id
    assert recommend.scan(db, TODAY)["next"] == 0


def test_the_suggestion_goes_away_once_the_request_was_started_by_hand(project):
    db, _, case_id = project
    recommend.scan(db, TODAY)
    catalog.prepare_request(db, case_id, today=TODAY)
    assert recommend.scan(db, TODAY)["expired"] == 1
    rec = next(r for r in db.recommendations() if r["plan_key"] == "collection_request")
    assert rec["state"] == "expired" and "이미 시작" in rec["dismiss_reason"]


def test_no_suggestion_before_the_preparation_is_finished(tmp_path):
    db, task_id, _ = build(tmp_path, prepared=False)
    try:
        runner, run_id = catalog.prepare(db, task_id, 2026, today=TODAY)
        runner.execute(run_id)                                                    # 승인 대기에서 멈춤
        assert recommend.scan(db, TODAY)["next"] == 0
    finally:
        db.close()


# ── 화면 ────────────────────────────────────────────────────────────

def spin(qapp, predicate, timeout=40):
    import time

    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert predicate()


def buttons(dialog, text):
    return [b for b in dialog.findChildren(QPushButton) if b.text() == text]


def test_workbox_follow_ups_are_closed_until_the_preparation_is_done(qapp, tmp_path):
    db, task_id, _ = build(tmp_path, prepared=False)
    try:
        dialog = workbox.WorkboxDialog(None, db, task_id, today=TODAY)
        dialog.run_button.click()
        spin(qapp, lambda: not dialog._busy)
        assert db.runs(task_id=task_id)[0]["state"] == "waiting_for_approval"
        assert buttons(dialog, "요청문 초안 만들기")[0].isEnabled()                # 초안·서식은 이미 있다
        dialog.close()
    finally:
        db.close()


def test_workbox_request_and_intake_flows(qapp, project, tmp_path, monkeypatch):
    db, task_id, case_id = project
    dialog = workbox.WorkboxDialog(None, db, task_id, today=TODAY)
    try:
        buttons(dialog, "요청문 초안 만들기")[0].click()
        spin(qapp, lambda: not dialog._busy)
        run = db.runs(case_id=case_id)[0]
        assert run["plan_key"] == "collection_request" and run["state"] == "waiting_for_approval"
        assert buttons(dialog, "승인하고 확정") and buttons(dialog, "확정하지 않기")
        buttons(dialog, "승인하고 확정")[0].click()
        spin(qapp, lambda: not dialog._busy)
        assert db.run(run["id"])["state"] == "completed"

        folder = reply_folder(tmp_path)
        monkeypatch.setattr(workbox, "choose_folder", lambda parent: str(folder))
        buttons(dialog, "회신 취합…")[0].click()
        spin(qapp, lambda: not dialog._busy)
        latest = db.runs(case_id=case_id)[0]
        assert latest["plan_key"] == "collection_intake" and latest["state"] == "completed"
        assert len(buttons(dialog, "열기")) == 6                                   # 취합본·보고서 등 만든 파일

        monkeypatch.setattr(workbox, "choose_folder", lambda parent: "")
        count = len(db.runs(case_id=case_id))
        buttons(dialog, "회신 취합…")[0].click()
        assert len(db.runs(case_id=case_id)) == count                              # 폴더를 안 고르면 아무것도 하지 않는다
        labels = [b.text() for b in dialog.findChildren(QPushButton)]
        assert any(label.startswith("반복 업무 준비") for label in labels)
    finally:
        dialog.close()
