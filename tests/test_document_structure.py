import zipfile
from app.ingest.parsers import parse


def test_hwpx_nested_table_text_is_not_repeated_by_parent_paragraph(tmp_path):
    path = tmp_path / "table.hwpx"
    xml = '<section><p><run><t>본문</t><tbl><tr><tc><p><run><t>고유금액820</t></run></p></tc></tr></tbl></run></p></section>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Contents/section0.xml", xml)
        archive.writestr("Contents/section10.xml", '<section><p><t>마지막</t></p></section>')
        archive.writestr("Contents/section2.xml", '<section><p><t>중간</t></p></section>')
    before = path.read_bytes()
    result = parse(path)
    assert result.ok
    assert result.text.count("고유금액820") == 1
    assert result.text.index("중간") < result.text.index("마지막")
    assert "Contents/section0.xml" in result.sections[0].locator
    assert path.read_bytes() == before


HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"


def cell(text, row, col, colspan=1, rowspan=1, header=0, inner=""):
    """한글이 실제로 쓰는 셀 구조: subList 안의 문단 + cellAddr + cellSpan."""
    return (f'<hp:tc header="{header}"><hp:subList><hp:p><hp:run><hp:t>{text}</hp:t>{inner}</hp:run></hp:p>'
            f'</hp:subList><hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
            f'<hp:cellSpan colSpan="{colspan}" rowSpan="{rowspan}"/></hp:tc>')


def table(*rows, caption=""):
    body = "".join(f"<hp:tr>{''.join(cells)}</hp:tr>" for cells in rows)
    if caption:
        body = (f'<hp:caption><hp:subList><hp:p><hp:run><hp:t>{caption}</hp:t></hp:run></hp:p>'
                f'</hp:subList></hp:caption>') + body
    return f'<hp:p><hp:run><hp:tbl>{body}</hp:tbl></hp:run></hp:p>'


def para(text):
    return f'<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>'


def parse_hwpx(tmp_path, *body):
    path = tmp_path / "document.hwpx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Contents/section0.xml",
                         f'<hs:sec xmlns:hs="{HP}" xmlns:hp="{HP}">{"".join(body)}</hs:sec>')
    return parse(path)


def rows_of(result):
    return [s for s in result.sections if s.kind == "table"]


def test_hwpx_table_rows_keep_first_row_and_unit_with_each_value(tmp_path):
    result = parse_hwpx(
        tmp_path, para("2025년 예산 요구"), para("(단위: 백만원)"),
        table([cell("사업", 0, 0), cell("요구액", 0, 1)],
              [cell("시범사업", 1, 0), cell("820", 1, 1)],
              [cell("운영비", 2, 0), cell("95", 2, 1)]))
    assert result.status == "ok"
    first, second, third = rows_of(result)
    assert first.text == "1열: 사업 | 2열: 요구액"
    assert second.text == "첫 행: 사업 | 요구액\n1열: 시범사업 | 2열: 820\n단위: 백만원"
    assert third.locator == "Contents/section0.xml · 표 1 · 3행"
    # 값이 다른 행의 항목과 섞이지 않는다.
    assert "820" not in third.text and "95" not in second.text
    assert result.text.count("820") == 1


def test_hwpx_merged_cells_fill_every_row_and_column_they_cover(tmp_path):
    result = parse_hwpx(tmp_path, table(
        [cell("구분", 0, 0, rowspan=2, header=1), cell("금액", 0, 1, colspan=2, header=1)],
        [cell("국비", 1, 1, header=1), cell("도비", 1, 2, header=1)],
        [cell("인건비", 2, 0, rowspan=2), cell("급여", 2, 1), cell("300", 2, 2)],
        [cell("수당", 3, 1), cell("40", 3, 2)],
    ))
    rows = rows_of(result)
    assert len(rows) == 4
    # 두 줄 머리글은 열마다 위아래를 이어 붙인다 — 제목 셀로 지정됐으므로 '머리글'이다.
    assert rows[2].text.startswith("머리글: 구분 | 금액 / 국비 | 금액 / 도비\n")
    assert "1열: 인건비 | 2열: 급여 | 3열: 300" in rows[2].text
    # 세로 병합된 '인건비'가 아래 행에서도 자기 구분으로 남는다.
    assert "1열: 인건비 | 2열: 수당 | 3열: 40" in rows[3].text
    assert "1열 병합 1x2" in rows[2].text and "병합" not in rows[3].text


def test_hwpx_unit_comes_from_caption_but_not_from_a_long_paragraph(tmp_path):
    long = "이 사업의 정산 기준은 다음과 같으며 금액은 모두 부가세를 포함한다 (단위: 원) 세부 내용은 붙임 참고"
    result = parse_hwpx(
        tmp_path, para(long),
        table([cell("항목", 0, 0), cell("값", 0, 1)], [cell("가", 1, 0), cell("1", 1, 1)]),
        table([cell("항목", 0, 0), cell("값", 0, 1)], [cell("나", 1, 0), cell("2", 1, 1)],
              caption="[표 2] 집행 현황 (단위: 천원)"))
    rows = rows_of(result)
    assert "단위" not in rows[1].text
    assert rows[3].text.endswith("단위: 천원")
    assert any("캡션" in s.locator for s in result.sections)


def test_hwpx_box_tables_and_nested_tables_stay_readable(tmp_path):
    inner = '<hp:tbl><hp:tr>' + cell("세부", 0, 0) + cell("7", 0, 1) + '</hp:tr></hp:tbl>'
    result = parse_hwpx(
        tmp_path,
        table([cell("제출기한은 9월 30일이다.", 0, 0)]),
        table([cell("바깥", 0, 0, inner=inner), cell("값", 0, 1)]))
    rows = rows_of(result)
    # 한 칸짜리 표는 글상자다 — 열 번호를 붙이지 않는다.
    assert rows[0].text == "제출기한은 9월 30일이다."
    assert rows[1].text == "1열: 바깥 | 2열: 값"
    assert rows[2].text == "1열: 세부 | 2열: 7" and "표 3" in rows[2].locator
    assert result.text.count("세부") == 1


def test_hwpx_text_after_inline_marks_and_absurd_spans_are_handled(tmp_path):
    marked = '<hp:p><hp:run><hp:t>기한<hp:tab/>9월<hp:lineBreak/>30일</hp:t></hp:run></hp:p>'
    result = parse_hwpx(tmp_path, marked,
                        table([cell("가", 0, 0, colspan=999999, rowspan=999999), cell("나", 0, 1)]))
    assert result.sections[0].text == "기한\t9월\n30일"
    assert result.status == "partial" and result.note
    assert rows_of(result)[0].text == "1열: 가 | 2열: 나"


ODT_HEAD = '''<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:text>'''
ODT_TAIL = '</office:text></office:body></office:document-content>'


def write_odt(path, body, manifest=None):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", ODT_HEAD + body + ODT_TAIL)
        if manifest:
            archive.writestr("META-INF/manifest.xml", manifest)


def test_odt_paragraphs_table_headers_repetition_and_locators(tmp_path):
    path = tmp_path / "document.odt"
    write_odt(path, '''<text:p>예산<text:s text:c="2"/>보고</text:p><table:table>
    <table:table-row><table:table-cell><text:p>구분</text:p></table:table-cell><table:table-cell><text:p>금액(백만원)</text:p></table:table-cell></table:table-row>
    <table:table-row table:number-rows-repeated="2"><table:table-cell><text:p>예산</text:p></table:table-cell><table:table-cell><text:p>820</text:p></table:table-cell></table:table-row></table:table>''')
    before = path.read_bytes()
    result = parse(path)
    assert result.ok and result.parser == "odt"
    assert "예산  보고" in result.text
    assert len([s for s in result.sections if s.kind == "table"]) == 3
    row = result.sections[-1]
    assert "금액(백만원)" in row.text and "820" in row.text
    assert "표 1 · 3행" in row.locator
    assert path.read_bytes() == before


def test_odt_encrypted_documents_are_identified(tmp_path):
    path = tmp_path / "encrypted.odt"
    write_odt(path, "<text:p>hidden</text:p>", '<manifest><encryption-data/></manifest>')
    assert parse(path).status == "encrypted"


def test_odt_repetition_is_bounded_and_reports_partial(tmp_path):
    path = tmp_path / "large.odt"
    write_odt(path, '''<table:table><table:table-row><table:table-cell table:number-columns-repeated="999999999"><text:p>value</text:p></table:table-cell></table:table-row></table:table>''')
    result = parse(path)
    assert result.status == "partial"
    assert result.note
    assert len(result.text) < 2_000_000
