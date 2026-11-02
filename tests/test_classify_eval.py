"""업무 분류 정확도 측정 도구."""

from __future__ import annotations

from pathlib import Path

from app.core import charter
from app.db import Database
from app.tools import classify_eval as ce


def build(tmp_path: Path) -> Path:
    path = tmp_path / "p.db"
    db = Database(path)
    db.init()
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES ('D:/전임자', 'local')").lastrowid
    charter.save_user_items(db, charter.parse_user_text("예산 편성\n  - 본예산\n  - 결산\n인사 관리"))
    for rel in ("예산 편성/본예산/a.xlsx", "예산 편성/본예산/b.xlsx", "인사 관리/c.hwp", "인사 관리/d.hwp",
                "잡다/e.hwp", "잡다/f.hwp"):
        db.con.execute(
            "INSERT INTO documents (source_id, path, filename, ext, hash, parse_status) VALUES (?, ?, ?, '.x', ?, 'ok')",
            (source, f"D:/전임자/{rel}", rel.rsplit("/", 1)[1], rel))
    db.close()
    return path


def test_read_gold_and_report(tmp_path):
    db_path = build(tmp_path)
    gold_file = tmp_path / "gold.csv"
    gold_file.write_text(
        "path,item,part\n"
        "예산 편성\\본예산\\a.xlsx,예산 편성,본예산\n"
        "예산 편성/본예산/b.xlsx,예산 편성,본예산\n"
        "인사 관리/c.hwp,인사 관리,\n"
        "인사 관리/d.hwp,예산 편성,\n"          # 일부러 틀린 정답
        "e.hwp,분장 밖,\n"
        "없는파일.hwp,예산 편성,\n", encoding="utf-8")
    gold = ce.read_gold(gold_file)
    assert len(gold) == 6 and gold[0].path == "예산 편성/본예산/a.xlsx"
    report = ce.run(db_path, gold)
    assert report.gold == 6 and report.found == 5 and report.missing == ["없는파일.hwp"]
    assert report.correct == 3 and report.decided == 4          # e.hwp는 판단 불가(모델 없음)
    assert round(report.accuracy, 2) == 0.6 and round(report.decided_accuracy, 2) == 0.75
    assert report.part_gold == 2 and report.part_correct == 2
    assert report.by_basis["이름 일치"]["n"] == 4
    assert {e["path"] for e in report.errors} == {"인사 관리/d.hwp", "e.hwp"}
    text = ce.render(report)
    assert "정확도 60.0%" in text and "근거별 정확도" in text


def test_main_writes_json_and_rejects_empty_gold(tmp_path, capsys):
    db_path = build(tmp_path)
    empty = tmp_path / "empty.csv"
    empty.write_text("path,item\n", encoding="utf-8")
    assert ce.main(["--db", str(db_path), "--gold", str(empty)]) == 2
    gold = tmp_path / "g.csv"
    gold.write_text("path,item\n인사 관리/c.hwp,인사 관리\n", encoding="utf-8")
    out = tmp_path / "r.json"
    assert ce.main(["--db", str(db_path), "--gold", str(gold), "--json", str(out)]) == 0
    assert '"accuracy": 1.0' in out.read_text(encoding="utf-8")
