import zipfile

import pytest

from app.tools.hwpx_audit import run


def make_file(tmp_path):
    source=tmp_path/'input'
    source.mkdir()
    path=source/'sample.hwpx'
    xml='<sec><p><t>2026. 9. 28. 검증 문서</t></p><tbl><tr><tc><cellAddr rowAddr="0" colAddr="0"/><cellSpan rowSpan="2" colSpan="1"/><subList><p><t>1부</t></p></subList></tc><tc><cellAddr rowAddr="0" colAddr="1"/><cellSpan rowSpan="1" colSpan="1"/><subList><p><t>09:00</t></p></subList></tc></tr><tr><tc><cellAddr rowAddr="1" colAddr="1"/><cellSpan rowSpan="1" colSpan="1"/><subList><p><t>09:10</t></p></subList></tc></tr></tbl></sec>'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('Contents/section0.xml',xml)
    return path


def test_single_document_audit_verifies_cells_chunks_and_persisted_scope(tmp_path):
    path=make_file(tmp_path)
    before=path.read_bytes()
    report=run(path,tmp_path/'output')
    assert report['source_bytes_unchanged'] and report['source_mtime_unchanged']
    assert path.read_bytes()==before
    assert report['scope_persisted_after_reopen']
    assert report['section_values_preserved_in_chunks']
    assert report['model_calls']==0
    checks=report['xml_checks']
    assert checks['physical_table_cells']==3
    assert checks['nonempty_cell_placements']==checks['matched_cell_placements']==4
    assert checks['nonempty_text_runs']==checks['matched_text_run_values']==4


def test_audit_never_writes_into_source_folder(tmp_path):
    path=make_file(tmp_path)
    with pytest.raises(ValueError,match='outside'):
        run(path,path.parent/'report')
    assert not (path.parent/'report').exists()


def test_existing_audit_is_not_overwritten(tmp_path):
    path=make_file(tmp_path)
    output=tmp_path/'existing'
    output.mkdir()
    keep=output/'report.json'
    keep.write_text('keep',encoding='utf-8')
    with pytest.raises(FileExistsError):
        run(path,output)
    assert keep.read_text(encoding='utf-8')=='keep'
