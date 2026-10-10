import io
import zipfile
from pathlib import Path

import pytest

from app.ingest.parsers import hwp, hwpx
from app.ingest.parsers.base import OK, PARTIAL


def package(path, parts, metadata=None):
    with zipfile.ZipFile(path, 'w') as z:
        for n, xml in enumerate(parts):
            z.writestr(f'Contents/section{n}.xml', xml)
        if metadata:
            z.writestr('Contents/content.hpf', metadata)
    return path


def cell(text, row, col, width=1, height=1):
    return (f'<tc><cellAddr rowAddr="{row}" colAddr="{col}"/>'
            f'<cellSpan rowSpan="{height}" colSpan="{width}"/>'
            f'<subList><p><run><t>{text}</t></run></p></subList></tc>')


def test_hwpx_counts_physical_cells_and_expanded_merge_separately(tmp_path):
    table = '<tbl><tr>' + cell('구분', 0, 0, height=2) + cell('시간', 0, 1) + '</tr><tr>' + cell('09:00', 1, 1) + '</tr></tbl>'
    path = package(tmp_path/'merge.hwpx', ['<sec><p><run><t>제목</t>' + table + '</run></p></sec>'])
    result = hwpx.parse(path)
    assert result.status == OK
    assert result.coverage['tables_visited'] == 1
    assert result.coverage['table_cells_visited'] == 3
    assert result.coverage['merged_cells_visited'] == 1
    assert result.coverage['table_rows_emitted'] == 2
    assert result.coverage['parsed_xml_inventory']['tc'] == 3
    assert '1열: 구분' in result.sections[-1].text
    assert not result.coverage['omissions']


def test_hwpx_character_limit_identifies_unvisited_parts(tmp_path, monkeypatch):
    path=package(tmp_path/'limit.hwpx', ['<sec><p><t>ABCDE</t></p></sec>','<sec><p><t>후속</t></p></sec>'])
    monkeypatch.setattr(hwpx,'MAX_CHARS',3)
    result=hwpx.parse(path)
    assert result.status == PARTIAL and result.char_count == 3
    assert result.coverage['xml_parts_unvisited'] == 1
    assert {x['reason'] for x in result.coverage['omissions']} == {'character_limit','unvisited_xml_parts'}


def test_hwpx_exact_character_boundary_and_blank_paragraphs_have_no_false_omission(tmp_path, monkeypatch):
    path=package(tmp_path/'exact.hwpx',['<sec><p><t>ABCDE</t></p><p><t/></p></sec>'])
    monkeypatch.setattr(hwpx,'MAX_CHARS',5)
    result=hwpx.parse(path)
    assert result.status == OK and not result.coverage['omissions']


def test_hwpx_failed_xml_part_is_distinct_from_unvisited_part(tmp_path):
    path=package(tmp_path/'broken.hwpx',['<sec>broken','<sec><p><t>not visited</t></p></sec>'])
    result=hwpx.parse(path)
    assert result.status == 'failed'
    assert result.coverage['xml_parts_visited']==1
    assert result.coverage['xml_parts_parsed']==0
    assert result.coverage['xml_parts_failed']==1
    assert result.coverage['xml_parts_unvisited']==1


def test_hwpx_cell_limit_is_checked_inside_wide_rows(tmp_path, monkeypatch):
    table='<tbl><tr>'+''.join(cell(str(c),0,c) for c in range(5))+'</tr></tbl>'
    path=package(tmp_path/'wide.hwpx',['<sec>'+table+'</sec>'])
    monkeypatch.setattr(hwpx,'MAX_CELLS_PER_SHEET',3)
    result=hwpx.parse(path)
    assert result.status == PARTIAL and result.coverage['table_cells_visited'] == 3
    assert result.coverage['omissions'][0]['reason'] == 'table_cell_limit'


def test_hwpx_exact_cell_limit_is_not_partial(tmp_path, monkeypatch):
    table='<tbl><tr>'+''.join(cell(str(c),0,c) for c in range(3))+'</tr></tbl>'
    path=package(tmp_path/'exactcells.hwpx',['<sec>'+table+'</sec>'])
    monkeypatch.setattr(hwpx,'MAX_CELLS_PER_SHEET',3)
    assert hwpx.parse(path).status == OK


def test_hwpx_sparse_coordinate_does_not_build_billion_element_grid(tmp_path):
    path=package(tmp_path/'sparse.hwpx',['<sec><tbl><tr>'+cell('value',10**9,10**9)+'</tr></tbl></sec>'])
    result=hwpx.parse(path)
    assert result.status == OK
    assert len(result.sections)==1 and 'value' in result.text
    assert '1000000001행' in result.sections[0].locator


def test_hwpx_opf_author_created_modified_dates_are_not_confused(tmp_path):
    metadata='<package><metadata><meta name="date">옛 표시 날짜</meta><meta name="creator">author</meta><meta name="CreatedDate">2019-01-16T00:47:53Z</meta><meta name="ModifiedDate">2026-09-30T07:00:00Z</meta></metadata></package>'
    path=package(tmp_path/'meta.hwpx',['<sec><p><t>본문 2026년</t></p></sec>'],metadata)
    result=hwpx.parse(path)
    assert result.meta.author=='author'
    assert result.meta.created=='2019-01-16T00:47:53Z'
    assert result.meta.modified=='2026-09-30T07:00:00Z'


def fake_ole(monkeypatch, text=None, broken=False, preview=None, extra=False):
    header=b'HWP Document File'.ljust(256,b'\0')
    streams={'FileHeader':header}
    if text is not None:
        payload=text.encode('utf-16-le')
        streams['BodyText/Section0']=(67 | (len(payload)<<20)).to_bytes(4,'little')+payload
    if broken: streams['BodyText/Section0']=RuntimeError('broken stream')
    if extra: streams['BodyText/Section1']=b''
    if preview is not None: streams['PrvText']=preview.encode('utf-16-le')
    class Ole:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def exists(self,key): return key in streams
        def listdir(self): return [key.split('/') for key in streams if '/' in key]
        def getproperties(self,*a,**k): return {}
        def openstream(self,key):
            value=streams['/'.join(key) if isinstance(key,list) else key]
            if isinstance(value,Exception): raise value
            return io.BytesIO(value)
    monkeypatch.setattr(hwp.olefile,'isOleFile',lambda p:True)
    monkeypatch.setattr(hwp.olefile,'OleFileIO',lambda p:Ole())


def test_hwp_limit_and_unvisited_stream_are_reported_as_partial(monkeypatch):
    fake_ole(monkeypatch,'ABCDE',extra=True)
    monkeypatch.setattr(hwp,'MAX_CHARS',3)
    result=hwp.parse(Path('synthetic.hwp'))
    assert result.status == PARTIAL and result.char_count == 3
    assert result.coverage['body_streams_unvisited']==1
    assert result.coverage['characters_emitted']==3
    assert result.coverage['omissions'][0]['reason']=='character_limit'


def test_hwp_preview_fallback_records_body_failure_and_structural_limit(monkeypatch):
    fake_ole(monkeypatch,broken=True,preview='미리보기')
    result=hwp.parse(Path('synthetic.hwp'))
    assert result.status == PARTIAL
    assert result.coverage['preview_fallback']
    assert result.coverage['body_streams_failed']==1
    assert result.coverage['characters_emitted']==len('미리보기')
    assert 'table_structure_not_preserved' in result.coverage['limitations']


def test_hwp_exact_last_paragraph_budget_is_not_a_known_omission(monkeypatch):
    fake_ole(monkeypatch,'ABCDE')
    monkeypatch.setattr(hwp,'MAX_CHARS',5)
    result=hwp.parse(Path('synthetic.hwp'))
    assert result.status == OK and not result.coverage['omissions']
