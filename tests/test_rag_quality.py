import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.db import Database
from app.tools import rag_quality


class Embed:
    embed_model='fake'
    def embed(self,texts):
        return [[1.,0.,0.] for _ in texts],None


def test_frozen_suite_rejects_changes_to_either_reserved_file(tmp_path):
    for name in ('dev.json','holdout.json'):
        (tmp_path/name).write_text('{"documents":[],"cases":[]}',encoding='utf-8')
    manifest={'files':{name:hashlib.sha256((tmp_path/name).read_bytes()).hexdigest()
                       for name in ('dev.json','holdout.json')}}
    (tmp_path/'frozen_manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    assert rag_quality.frozen_suite('dev',tmp_path)[0]['cases']==[]
    (tmp_path/'holdout.json').write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='동결'):
        rag_quality.frozen_suite('dev',tmp_path)


def test_fixtures_are_frozen_and_topics_are_separate():
    dev,manifest=rag_quality.frozen_suite('dev')
    holdout,_=rag_quality.frozen_suite('holdout')
    assert len(dev['cases'])==12 and len(holdout['cases'])==24
    assert not ({c['question'] for c in dev['cases']} & {c['question'] for c in holdout['cases']})
    assert not ({d['filename'] for d in dev['documents']} & {d['filename'] for d in holdout['documents']})
    assert len(manifest['baseline_code'])==3


def test_quality_project_is_repeatable_but_does_not_overwrite_changed_original(tmp_path):
    dev,manifest=rag_quality.frozen_suite('dev')
    digest=manifest['files']['dev.json']
    db=rag_quality.prepare(tmp_path,dev,digest,Embed())
    expected=db.counts()['chunks']
    assert db.unembedded_chunk_count('fake')==0
    doc=db.con.execute('SELECT path FROM documents LIMIT 1').fetchone()[0];db.close()
    db=rag_quality.prepare(tmp_path,dev,digest,Embed())
    assert db.counts()['chunks']==expected;db.close()
    Path(doc).write_text('사용자가 변경한 자료',encoding='utf-8')
    with pytest.raises(ValueError,match='평가 원문이 변경'):
        rag_quality.prepare(tmp_path,dev,digest,Embed())
    assert Path(doc).read_text(encoding='utf-8')=='사용자가 변경한 자료'


def test_existing_project_is_refused_before_initialization_or_migration(tmp_path):
    target=tmp_path/'project.db'
    with sqlite3.connect(target) as con:
        con.execute('CREATE TABLE marker(value TEXT)')
        con.execute("INSERT INTO marker VALUES ('원래 자료')")
    before=target.read_bytes()
    dev,manifest=rag_quality.frozen_suite('dev')
    with pytest.raises(ValueError,match='평가용'):
        rag_quality.prepare(tmp_path,dev,manifest['files']['dev.json'],Embed())
    assert target.read_bytes()==before


def test_search_only_is_not_counted_as_correct_abstention(tmp_path,monkeypatch):
    from app.search.rag import Answer
    monkeypatch.setattr(rag_quality,'ask',lambda *_args,**_kwargs:Answer('q','검색 결과',True,generation={'mode':'search_only'}))
    result=rag_quality.run_case(None,{'id':'absent','question':'q','expect':'withhold'},None)
    assert not result.passed
