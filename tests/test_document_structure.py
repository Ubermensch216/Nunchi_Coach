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
