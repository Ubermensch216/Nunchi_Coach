"""적은 핵심 정보로 일하고, 새 자료의 영향을 보고, 품질을 계속 잰다 (규제개혁 개선계획서 P2).

  · 긴 구획은 먼저 볼 것만 펴고 나머지를 접는다(준비 항목 708건, R05). 지우지 않는다.
  · 다시 분석하면 자동으로 읽은 값의 전후를 남기고, 기한·제출처·절차가 바뀌면 먼저 알린다.
  · 자료가 엇갈리면 어느 단계에 영향을 주고 누구에게 확인하는지 말한다.
  · 품질 관문이 비교연구의 오류 꼴을 정답 없이 잰다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.core import changes, continuity, docrole, reconstruction as rc, task_manual, workcard
from app.core import workstate as ws
from app.db import Database
from app.tools import quality_gate

ROOT = r"D:\전임자\[업무]"
BASIS = date(2026, 10, 14)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '실적 제출')")
    yield database
    database.close()


def _doc(db, relative, lines, year=2026, role=docrole.DIRECTIVE):
    source = db.add_source(ROOT)
    name = relative.rsplit("\\", 1)[-1]
    doc = db.upsert_document(source, {
        "path": ROOT + "\\" + relative, "filename": name, "ext": Path(name).suffix, "parse_status": "ok",
        "hash": relative, "eff_year": year, "eff_date": f"{year}-03-01", "eff_precision": "day", "eff_date_kind": "body"})
    db.replace_sections(doc, [("paragraph", i, f"{i}문단", line, {}) for i, line in enumerate(lines, 1)])
    db.index_document(doc, name, ROOT + "\\" + relative, "", "\n".join(lines))
    db.con.execute("INSERT OR IGNORE INTO task_docs(task_id, doc_id, origin) VALUES (1, ?, 'ai')", (doc,))
    db.con.execute("INSERT OR REPLACE INTO doc_roles(doc_id, role, basis, confidence, decided_by) "
                   "VALUES (?, ?, '', 'medium', 'ai')", (doc, role))
    return doc


# ── 접기 ────────────────────────────────────────────────────────────

def test_a_long_section_shows_what_to_look_at_first_and_folds_the_rest(db):
    # 부처 검토의견 서식의 담당자 표처럼 행마다 다른 담당이 적힌다.
    lines = [f"첫 행: 구분 | 담당자\n1열: 과제{n} | 2열: 사무관 {chr(0xAC00 + n)}" for n in range(15)]
    lines += ["제출 서식 : 실적서(붙임1)", "구비서류 : 증빙 공문"]
    _doc(db, r"2026\요청.hwpx", lines)
    rc.restore(db, BASIS, statements=False)
    section = next(s for s in task_manual.build(db, 1).sections if s.key == "prepare")
    assert len(section.lines) == 10 and section.more
    assert section.lines[0].startswith("서식") and section.lines[1].startswith("준비 자료")
    assert len(section.lines) + len(section.more) == 17       # 지우지 않는다


def test_a_short_section_is_not_folded(db):
    _doc(db, r"2026\요청.hwpx", ["제출 서식 : 실적서(붙임1)", "구비서류 : 증빙 공문"])
    rc.restore(db, BASIS, statements=False)
    section = next(s for s in task_manual.build(db, 1).sections if s.key == "prepare")
    assert section.more == [] and len(section.lines) == 2


# ── 새 자료의 영향 ──────────────────────────────────────────────────

def test_a_changed_deadline_is_recorded_and_raised(db):
    doc = _doc(db, r"2026\요청.hwpx", ["제출기한 : 2026. 11. 4.", "제출처 : 가나시 규제혁신과"])
    rc.restore(db, BASIS, statements=False)
    assert changes.latest(db, 1) == []                        # 처음 분석은 남기지 않는다
    rc.restore(db, BASIS, statements=False)
    assert changes.latest(db, 1) == []                        # 같은 자료면 바뀐 것이 없다

    db.replace_sections(doc, [("paragraph", 1, "1문단", "제출기한 : 2026. 11. 11.", {}),
                              ("paragraph", 2, "2문단", "제출처 : 가나시 규제혁신과", {}),
                              ("paragraph", 3, "3문단", "구비서류 : 실적 증빙", {})])
    totals = rc.restore(db, BASIS, statements=False)
    found = changes.latest(db, 1)
    assert totals["changes"] == len(found) == 2
    assert {(c["change"], c["slot"], c["before"], c["after"]) for c in found} == {
        ("changed", "제출기한", "2026-11-04", "2026-11-11"), ("added", "구비서류", "", "실적 증빙")}

    first = continuity.next_actions(db, 1)
    moved = next(a for a in first if a["text"].startswith("[바뀐 자료]"))
    assert "기한 · 제출기한: 2026-11-04 → 2026-11-11" in moved["text"]
    basis = next(s for s in task_manual.build(db, 1).sections if s.key == "basis")
    assert basis.lines[0].startswith("지난 분석 이후 바뀐 것(") and "바뀜 1" in basis.lines[0]


# ── 엇갈림 카드 ─────────────────────────────────────────────────────

def test_a_conflict_says_which_step_it_holds_and_whom_to_ask(db):
    _doc(db, r"2026\요청.hwpx", ["절차 : 자료 취합 → 실적서 작성 → 시에 제출", "제출기한 : 2026. 11. 4."])
    _doc(db, r"2026\요청(수정).hwpx", ["제출기한 : 2026. 11. 7."])
    rc.restore(db, BASIS, statements=False)
    gap = next(g for g in db.info_gaps(1) if g["kind"] == "conflict")
    assert "'시에 제출' 단계를 끝내는 데 필요합니다" in gap["detail"]
    assert "요청 공문을 보낸 부서에 이번 회차 기한을 물으세요" in gap["next_action"]
    assert "'시에 제출' 단계의 완료 기준으로 쓰입니다" in gap["next_action"]


# ── 품질 관문 ───────────────────────────────────────────────────────

def test_the_gate_passes_clean_work_and_catches_a_placeholder(db, tmp_path):
    _doc(db, r"2026\요청.hwpx", ["제출기한 : 2026. 11. 4.", "제출처 : 가나시 규제혁신과"])
    rc.restore(db, BASIS, statements=False)
    report = quality_gate.measure(db, [{"task": "실적 제출", "must_contain": ["가나시"], "must_not_contain": ["0000과"]}])
    assert report["passed"] and all(g["passed"] for g in report["gates"])

    item = db.con.execute("SELECT id FROM task_items WHERE kind = 'recipient'").fetchone()["id"]
    db.con.execute("UPDATE task_items SET content = '시·도/시·군·구 0000과' WHERE id = ?", (item,))
    report = quality_gate.measure(db, [{"task": "실적 제출", "must_not_contain": ["0000과"]}])
    gate = next(g for g in report["gates"] if g["key"] == "G3")
    assert not gate["passed"] and gate["examples"] == ["실적 제출: 제출처 = 시·도/시·군·구 0000과"]
    assert report["truth"][0]["problems"] == ["있으면 안 되는 말: 0000과"] and not report["passed"]


def test_the_gate_catches_a_past_procedure_offered_as_doable(db):
    _doc(db, r"2024\계획.hwpx", ["처리 절차 : 자료 취합 → 실적서 작성 → 시에 제출"], year=2024, role=docrole.PLAN)
    rc.restore(db, BASIS, statements=False)
    assert next(g for g in quality_gate.measure(db)["gates"] if g["key"] == "G1")["passed"]
    # 화면의 안전장치를 끄면(지난 회차 절차를 지금 절차로 잘못 저장하면) 관문이 잡는다.
    db.con.execute("UPDATE work_steps SET applicability = ? WHERE procedure_kind = 'stated'", (ws.CURRENT,))
    first = next(a for a in continuity.next_actions(db, 1) if a["type"] == "do")
    assert "진행할 수 있습니다" in first["text"]
    db.con.execute("UPDATE work_steps SET applicability = ? WHERE procedure_kind = 'stated'", (ws.PAST,))
    assert next(g for g in quality_gate.measure(db)["gates"] if g["key"] == "G1")["passed"]


def test_the_gate_command_line_reports_and_sets_the_exit_code(db, tmp_path, capsys):
    _doc(db, r"2026\요청.hwpx", ["제출기한 : 2026. 11. 4."])
    rc.restore(db, BASIS, statements=False)
    db.close()
    truth = tmp_path / "기준.json"
    truth.write_text(json.dumps([{"task": "실적 제출", "must_contain": ["없는 말"]}], ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "결과.json"
    code = quality_gate.main(["--db", str(tmp_path / "p.db"), "--truth", str(truth), "--json", str(out)])
    assert code == 1 and "판정: 실패" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["truth"][0]["problems"] == ["있어야 할 말이 없음: 없는 말"]
