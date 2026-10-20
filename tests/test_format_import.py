"""서식 파일에서 본문 서식을 읽어 산출물에 입히는 시험.

실물 문서는 저장소에 두지 않는다. 실물의 구조를 본뜬 합성 파일(최소 XML)만 쓴다.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.studio import format_import, layout, markers, render
from app.studio.templates import DEFAULT_FORMAT

HEAD = ('<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
        'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
        '<hh:fontfaces><hh:fontface lang="HANGUL"><hh:font id="0" face="휴먼명조"/>'
        '<hh:font id="1" face="HY헤드라인M"/></hh:fontface></hh:fontfaces><hh:charProperties>{chars}'
        '</hh:charProperties><hh:paraProperties>{paras}</hh:paraProperties></hh:head>')
SECTION = ('<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
           'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">{body}</hs:sec>')


def _char(key: int, font: int, height: int, bold: bool = False, spacing: int = 0) -> str:
    return (f'<hh:charPr id="{key}" height="{height}"><hh:fontRef hangul="{font}"/><hh:ratio hangul="100"/>'
            f'<hh:spacing hangul="{spacing}"/>{"<hh:bold/>" if bold else ""}</hh:charPr>')


def _para(key: int, line: int, align: str = "JUSTIFY", prev: int = 0, switched: bool = False) -> str:
    inner = (f'<hh:margin><hc:intent value="0" unit="HWPUNIT"/><hc:prev value="{prev}" unit="HWPUNIT"/></hh:margin>'
             f'<hh:lineSpacing type="PERCENT" value="{line}"/>')
    if switched:        # 여백·줄간격이 hp:switch 안에 든 꼴
        inner = f"<hp:switch><hp:case>{inner}</hp:case></hp:switch>"
    return f'<hh:paraPr id="{key}"><hh:align horizontal="{align}"/>{inner}</hh:paraPr>'


def _p(text: str, key: int) -> str:
    return f'<hp:p paraPrIDRef="{key}"><hp:run charPrIDRef="{key}"><hp:t>{text}</hp:t></hp:run></hp:p>'


def _hwpx(path: Path, body: str) -> Path:
    chars = _char(0, 1, 2000, bold=True) + _char(1, 1, 1600, bold=True) + _char(2, 0, 1500) + \
        _char(3, 0, 1500, spacing=-8) + _char(4, 0, 1200)
    paras = _para(0, 160, "CENTER") + _para(1, 180, prev=1000) + _para(2, 171, switched=True) + \
        _para(3, 160) + _para(4, 150)
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("mimetype", "application/hwp+zip")
        package.writestr("Contents/header.xml", HEAD.format(chars=chars, paras=paras))
        package.writestr("Contents/section0.xml", SECTION.format(body=body))
    return path


REPORT = (
    '<hp:p paraPrIDRef="4"><hp:run charPrIDRef="4"><hp:tbl><hp:tr>'
    "<hp:tc><hp:subList>" + _p("기안자", 4) + "</hp:subList></hp:tc>"
    "<hp:tc><hp:subList>" + _p("결재권자", 4) + "</hp:subList></hp:tc>"
    "</hp:tr></hp:tbl></hp:run></hp:p>"
    + _p("2026년 수질 관리 추진 계획", 0)
    + _p("□ 추진 배경", 1) + _p(" ○ 수질 기준이 강화됨", 2) + _p(" ○ 측정 지점을 늘려야 함", 2)
    + _p("   - 12개 지점에서 20개 지점으로", 3) + _p("□ 추진 일정", 1) + _p(" ❍ 3월까지 계획을 확정함", 2)
    + _p(" ※ 예산은 별도 협의", 4))


def test_marker_levels_ignore_the_glyph_shape():
    assert markers.classify(" ❍ 내용").key == markers.classify("ㅇ 내용").key == "item"
    assert markers.classify("□ 추진 배경").rest == "추진 배경"
    assert markers.classify("9월 30일까지") is None
    assert markers.lead_spaces("　 - x") == 3
    assert [markers.ordinal_glyph("ga", n) for n in (1, 2)] == ["가.", "나."]
    assert markers.ordinal_glyph("chapter", 2) == "Ⅱ."


def test_report_format_is_read_from_hwpx(tmp_path: Path):
    analysis = format_import.analyze(_hwpx(tmp_path / "추진 계획.hwpx", REPORT))

    levels = {level.key: level for level in analysis.levels}
    assert analysis.doc_kind == "report" and analysis.headings == ["추진 배경", "추진 일정"]
    assert (levels["title"].char.size, levels["title"].para.align) == (20, "center")
    assert (levels["section"].char.font, levels["section"].char.bold, levels["section"].para.before) == (
        "HY헤드라인M", True, 10)
    # 같은 단계의 기호가 섞여 있어도(○·❍) 한 단계로 묶고, 줄간격은 5% 단위로 맞춘다.
    assert (levels["item"].count, levels["item"].glyph, levels["item"].lead, levels["item"].para.line) == (
        3, "○", 1, 170)
    assert (levels["sub"].lead, levels["sub"].char.spacing) == (3, -8)
    # 결재란 표는 분석하지 않는다.
    assert all("기안자" not in level.sample for level in analysis.levels)
    assert any("문서 틀 표 1개" in note for note in analysis.notes)

    fmt = format_import.studio_format(analysis)
    assert (fmt["heading"]["glyph"], fmt["heading"]["font"], fmt["heading"]["size"]) == ("□", "HY헤드라인M", 16)
    assert [(level["glyph"], level["lead"]) for level in fmt["levels"]] == [("○", 1), ("-", 3)]
    assert fmt["note"]["glyph"] == "※" and fmt["source"]["file"] == "추진 계획.hwpx"
    # 원본의 글은 서식에 남기지 않는다.
    assert "수질" not in str(fmt)


def test_format_is_applied_to_the_draft(tmp_path: Path):
    fmt = format_import.studio_format(format_import.analyze(_hwpx(tmp_path / "a.hwpx", REPORT)))
    draft = "# 보고\n\n## 1. 보고 배경\n- 첫 문장 [1]\n  - 세부\n## 2. 계획\n- 둘째\n> 확인 필요: 근거 없음\n"

    texts = [line.text for line in layout.lay_out(draft, fmt)]
    assert texts == ["보고", "□ 보고 배경", " ○ 첫 문장 [1]", "   - 세부", "□ 계획", " ○ 둘째",
                     " ※ 확인 필요: 근거 없음"]

    # 내보낸 한글 파일에 서식의 글꼴·기호가 들어가고, 그 파일에서 같은 서식을 다시 읽어 낸다.
    target = tmp_path / "out.hwpx"
    target.write_bytes(render.to_hwpx(draft, fmt, "보고"))
    again = {level.key: level for level in format_import.analyze(target).levels}
    assert (again["section"].char.font, again["section"].char.size) == ("HY헤드라인M", 16)
    assert (again["item"].lead, again["sub"].char.spacing) == (1, -8)
    for kind in ("docx", "pdf", "xlsx"):
        assert render.RENDERERS[kind](draft, fmt, "보고")


def test_numbered_levels_are_renumbered():
    fmt = {**DEFAULT_FORMAT, "heading": {**DEFAULT_FORMAT["heading"], "glyph": "Ⅰ", "key": "chapter"},
           "levels": [{"glyph": "1.", "key": "num", "lead": 1}, {"glyph": "가.", "key": "ga", "lead": 3}]}
    draft = "## 1. 배경\n- 하나\n  - 세부\n  - 세부\n- 둘\n  - 세부\n## 2. 계획\n- 셋\n"
    assert [line.text for line in layout.lay_out(draft, fmt)] == [
        "Ⅰ. 배경", " 1. 하나", "   가. 세부", "   나. 세부", " 2. 둘", "   가. 세부", "Ⅱ. 계획", " 1. 셋"]
    # 기호가 없는 기본 서식은 항목 제목을 적힌 그대로 둔다.
    assert layout.lay_out("## 1. 배경", DEFAULT_FORMAT)[0].text == "1. 배경"


ODT = ('<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
       'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
       'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
       'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
       'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">'
       '<office:automatic-styles>'
       '<style:style style:name="P1" style:family="paragraph"><style:paragraph-properties fo:line-height="123%" '
       'fo:margin-left="0.63cm" fo:text-indent="-0.63cm"/><style:text-properties style:font-name-asian="돋움체" '
       'fo:font-size="12pt"/></style:style>'
       '<style:style style:name="P2" style:family="paragraph" style:parent-style-name="P1">'
       '<style:text-properties fo:letter-spacing="0.48pt"/></style:style>'
       '</office:automatic-styles><office:body><office:text>'
       '<table:table><table:table-row><table:table-cell table:protected="true">'
       '<text:p text:style-name="P1">등록번호</text:p></table:table-cell></table:table-row></table:table>'
       '<text:p text:style-name="P1">머리말은 본문이 아니다</text:p>'
       '<text:p text:style-name="P1"><text:bookmark-start text:name="NormalField¡본문¡본문을 입력하십시오"/>'
       '1. 귀 기관의 무궁한 발전을 기원합니다.</text:p>'
       '<text:p text:style-name="P1">2. 아래와 같이 알려드립니다.</text:p>'
       '<text:p text:style-name="P2"><text:s text:c="2"/>가. 행 사 명: 나이트 마켓</text:p>'
       '<text:p text:style-name="P2"><text:s text:c="2"/>나. 일시: 9. 30.'
       '<text:bookmark-end text:name="NormalField¡본문¡본문을 입력하십시오"/></text:p>'
       '<text:p text:style-name="P1">발신명의</text:p>'
       '</office:text></office:body></office:document-content>')


def test_official_format_is_read_from_the_body_field_of_odt(tmp_path: Path):
    path = tmp_path / "알림.odt"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("content.xml", ODT)
    analysis = format_import.analyze(path)

    levels = {level.key: level for level in analysis.levels}
    assert analysis.doc_kind == "official" and analysis.headings == [] and set(levels) == {"num", "ga"}
    # ODT 123%는 한글 160%로, 자간 0.48pt ÷ 12pt는 4%로 옮긴다.
    assert (levels["num"].char.font, levels["num"].para.line, levels["num"].para.indent) == ("돋움체", 160, -17.9)
    assert (levels["ga"].lead, levels["ga"].char.spacing) == (2, 4)
    fmt = format_import.studio_format(analysis)
    assert (fmt["heading"]["key"], fmt["levels"][0]["key"], fmt["heading"]["hanging"]) == ("num", "ga", 17.9)


def test_unreadable_files_say_why(tmp_path: Path):
    old = tmp_path / "옛.hwp"
    old.write_bytes(b"\xd0\xcf\x11\xe0" + b"0" * 20)
    with pytest.raises(format_import.FormatError, match="HWPX"):
        format_import.analyze(old)
    text = tmp_path / "글.hwpx"
    text.write_text("그냥 글", encoding="utf-8")
    with pytest.raises(format_import.FormatError, match="HWPX · ODT"):
        format_import.analyze(text)
    with pytest.raises(format_import.FormatError, match="서식을 찾지 못했습니다"):
        format_import.analyze(_hwpx(tmp_path / "빈.hwpx", ""))


# ── 화면: 서식 등록 → 산출물에 적용 → 삭제 ───────────────────────────
@pytest.fixture
def studio(tmp_path: Path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.core import notebook
    from app.db import Database
    from app.studio import outputs, templates
    from app.studio.compose import Draft
    from app.ui import theme
    from app.ui.views.notebooks.studio_panel import StudioPanel

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    db = Database(tmp_path / "project.db")
    db.init()
    book = notebook.create(db, "수질 업무노트")
    template = templates.all_templates(db)[0]
    draft = Draft("수질 보고", "# 수질 보고\n\n## 1. 보고 배경\n- 기준이 강화됨\n", [], {})
    keys = [outputs.save_draft(db, book, template, draft) for _ in range(2)]
    panel = StudioPanel(db)
    panel.set_notebook(book, True)
    yield panel, db, keys
    panel.close()
    db.close()


def test_template_is_registered_from_a_file_and_applied(studio, tmp_path: Path):
    from app.studio import outputs, templates
    from app.ui.views.notebooks.studio_panel import TemplateDialog

    panel, db, keys = studio
    dialog = TemplateDialog(db)
    before = len(templates.all_templates(db))
    dialog.import_file(str(_hwpx(tmp_path / "추진 계획.hwpx", REPORT)))
    # 가져온 것은 초안이다 — 저장을 눌러야 등록된다. 대제목 글이 항목이 된다.
    assert len(templates.all_templates(db)) == before and "[저장]" in dialog.state.text()
    assert dialog._titles() == ["추진 배경", "추진 일정"] and dialog.name.text() == "추진 계획"
    assert "□ 추진 배경" in dialog.preview.toPlainText() and "문서 틀 표" in dialog.notes.text()
    # 저장하기 전에 사람이 고친다: 첫 목록 단계의 기호를 ○에서 1.로.
    dialog.style_slot.setCurrentIndex(dialog.style_slot.findData("levels.0"))
    dialog.glyph.setText("1.")
    dialog.save_button.click()
    saved = templates.all_templates(db)[-1]
    assert len(templates.all_templates(db)) == before + 1 and not saved.builtin
    assert (saved.format["levels"][0]["key"], saved.format["heading"]["font"]) == ("num", "HY헤드라인M")
    assert saved.format["source"]["file"] == "추진 계획.hwpx"
    dialog.close()

    # 등록한 서식의 모양을 이미 엮은 산출물에 입힌다. 글은 그대로다.
    panel.refresh(force=True)
    panel.outputs.setCurrentRow(0)
    panel._apply_format(saved)
    row = outputs.get(db, panel._output_id())
    assert outputs.payload(row)["markdown"].startswith("# 수질 보고")
    shown = panel.output_view.toPlainText()
    assert "□ 보고 배경" in shown and "1. 기준이 강화됨" in shown and "#" not in shown


def test_each_output_row_has_a_delete_icon(studio, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QPushButton

    from app.studio import outputs

    panel, db, keys = studio
    assert panel.outputs.count() == 2
    buttons = [panel.outputs.itemWidget(panel.outputs.item(i)).findChild(QPushButton) for i in range(2)]
    assert all(button is not None and "삭제" in button.toolTip() for button in buttons)
    # 묻고 지운다. 고르지 않은 줄의 휴지통도 그 줄의 것을 지운다.
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    buttons[1].click()
    assert len(outputs.outputs(db, 1)) == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    buttons[1].click()
    assert [row["id"] for row in outputs.outputs(db, 1)] == [max(keys)]
    # 보관한 업무노트에서는 지울 수 없다 — 휴지통이 없다.
    panel.set_notebook(1, False)
    assert panel.outputs.itemWidget(panel.outputs.item(0)).findChild(QPushButton) is None


def test_each_memo_row_has_a_delete_icon(studio, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QPushButton

    from app.core import notebook

    panel, db, _keys = studio
    notebook.add_memo(db, 1, "남길 메모", "내용")
    notebook.add_memo(db, 1, "지울 메모", "내용")
    panel.refresh(force=True)
    trash = panel.notes.itemWidget(panel.notes.item(0)).findChild(QPushButton)
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda _parent, _title, text, *a, **k: asked.append(text) or QMessageBox.StandardButton.Yes)
    trash.click()
    assert "지울 메모" in asked[0] and "지식 카드" not in asked[0]
    assert [row["title"] for row in panel.note_rows()] == ["남길 메모"] and panel.notes.count() == 1
    # 업무 가이드에 게시한 지식이 딸려 있으면 지우기 전에 그 사실을 알린다.
    note = panel.note_rows()[0]["id"]
    db.con.execute("INSERT INTO knowledge_cards(note_id,note_revision,body,evidence,reviewer,applicability,"
                   "published_at) VALUES (?,1,'b','[]','r','a',datetime('now'))", (note,))
    panel.notes.itemWidget(panel.notes.item(0)).findChild(QPushButton).click()
    assert "지식 카드 1건" in asked[1] and panel.notes.count() == 0
    assert db.con.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone()[0] == 0
