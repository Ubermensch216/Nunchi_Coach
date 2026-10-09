"""동결한 합성 문항으로 상충 근거·과잉 보류를 재는 독립 실행 도구.

python -m app.tools.rag_quality --suite dev --folder .test_runs/quality-dev --phase baseline --json .test_runs/quality-baseline.json
python -m app.tools.rag_quality --suite holdout --folder .test_runs/quality-holdout --phase candidate --json .test_runs/quality-holdout.json

기관 자료를 읽거나 기존 프로젝트를 수정하지 않는다. holdout 결과를 보고
코드를 재조정했다면 그 결과는 최종 성능이 아니라 개발 측정으로 분류해야 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..ai.settings import project_client
from ..core.chunking import build_chunks
from ..db import Database
from ..ingest.parsers import parse
from ..search.rag import ask
from ..search.vector import pack
from . import rag_report

FIXTURES = Path(__file__).resolve().parents[2] / 'tests/fixtures/rag_quality_2026_10_09'


def frozen_suite(name, root=FIXTURES):
    if name not in ('dev', 'holdout'):
        raise ValueError('dev 또는 holdout을 선택하세요.')
    manifest = json.loads((root / 'frozen_manifest.json').read_text(encoding='utf-8'))
    for filename, expected in manifest['files'].items():
        if hashlib.sha256((root / filename).read_bytes()).hexdigest() != expected:
            raise ValueError(f'동결한 평가 파일이 변경됐습니다: {filename}')
    return json.loads((root / (name + '.json')).read_text(encoding='utf-8')), manifest


def prepare(folder, suite, suite_hash, client):
    folder = Path(folder).resolve()
    target = folder / 'project.db'
    if target.exists():
        try:
            with sqlite3.connect(target.as_uri()+'?mode=ro',uri=True) as existing:
                marked = existing.execute("SELECT value FROM meta WHERE key='quality_suite_hash'").fetchone()
                if marked is None or marked[0] != suite_hash:
                    raise ValueError('기존 프로젝트를 평가용으로 덮어쓸 수 없습니다.')
        except sqlite3.Error as exc:
            raise ValueError('평가용으로 만든 DB만 다시 사용할 수 있습니다.') from exc
    folder.mkdir(parents=True, exist_ok=True)
    db = Database(folder / 'project.db')
    try:
        db.init()
        known = db.get_meta('quality_suite_hash')
        if known and known != suite_hash:
            raise ValueError('다른 평가셋으로 만든 폴더입니다. 새 폴더를 사용하세요.')
        if not known and db.counts()['documents']:
            raise ValueError('기존 프로젝트를 평가용으로 덮어쓸 수 없습니다.')
        source = folder / '자료'
        source.mkdir(exist_ok=True)
        source_id = db.add_source(source)
        with db.transaction():
            for spec in suite['documents']:
                # 고정 자료의 단일 파일명만 허용한다.
                if Path(spec['filename']).name != spec['filename']:
                    raise ValueError('평가 자료의 파일명이 올바르지 않습니다.')
                path = source / spec['filename']
                content = spec['text'].encode('utf-8')
                if path.exists():
                    if path.read_bytes() != content:
                        raise ValueError(f'평가 원문이 변경됐습니다: {path.name}')
                else:
                    path.write_bytes(content)
                previous = db.con.execute('SELECT id FROM documents WHERE path=?', (str(path),)).fetchone()
                if previous:
                    continue
                result = parse(path)
                if not result.ok:
                    raise ValueError(f'평가 원문을 읽을 수 없습니다: {path.name}')
                doc_id = db.upsert_document(source_id, {
                    'path': str(path), 'filename': path.name, 'ext': path.suffix,
                    'hash': hashlib.sha256(content).hexdigest(), 'parse_status': result.status,
                    'char_count': result.char_count, 'eff_year': spec['year']})
                db.replace_sections(doc_id, [(s.kind,s.ordinal,s.locator,s.text) for s in result.sections])
                db.index_document(doc_id,path.name,str(path),'',result.text)
                db.replace_document_chunks(doc_id,build_chunks([(s.kind,s.locator,s.text) for s in result.sections]))
            db.set_meta('quality_suite_hash',suite_hash)
        pending = db.con.execute('SELECT c.id,c.text FROM chunks c LEFT JOIN embeddings e '
                                 'ON e.chunk_id=c.id AND e.model=? WHERE e.chunk_id IS NULL ORDER BY c.id',
                                 (client.embed_model,)).fetchall()
        for start in range(0,len(pending),32):
            batch = pending[start:start+32]
            vectors,error = client.embed([r['text'] for r in batch])
            if error or vectors is None or len(vectors) != len(batch):
                raise ValueError(error or '평가 자료 임베딩이 완성되지 않았습니다.')
            with db.transaction():
                for row,vector in zip(batch,vectors):
                    db.save_chunk_embedding(row['id'],client.embed_model,len(vector),pack(vector))
        from ..core.child_index import embed_children
        for row in db.con.execute('SELECT id FROM documents ORDER BY id').fetchall():
            db.ensure_child_chunks(row['id'])
            error = embed_children(db, row['id'], client)
            if error:
                raise ValueError(error)
        return db
    except BaseException:
        db.close()
        raise


def run_case(db,case,client):
    started = time.monotonic()
    answer = ask(db,case['question'],client=client)
    outcome = rag_report.Outcome(case['id'],case['question'],case['expect'],answer.withheld,
        round(time.monotonic()-started,3),[c.filename for c in answer.citations],answer.text,answer.error)
    outcome.path = answer.generation.get('withheld') or answer.generation.get('rule_answer') or (
        'search_only' if answer.generation.get('mode')=='search_only' else 'model')
    # 모델이 준비되지 않은 검색 결과를 옳은 보류로 채점하지 않는다.
    if outcome.path=='search_only':
        outcome.error='생성·의미 색인이 준비되지 않아 품질을 측정할 수 없다'
    outcome.failures = rag_report._judge(case,outcome)
    return outcome


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',choices=['dev','holdout'],required=True)
    parser.add_argument('--phase',choices=['baseline','candidate'],required=True)
    parser.add_argument('--folder',type=Path,required=True)
    parser.add_argument('--json',type=Path,required=True)
    parser.add_argument('--repeat',type=int,default=3)
    parser.add_argument('--fixtures',type=Path,default=FIXTURES)
    args=parser.parse_args(argv)
    rag_report._force_utf8()
    if args.repeat < 1 or args.repeat > 10 or (args.suite=='holdout' and args.phase=='baseline'):
        parser.error('반복은 1~10회이며 holdout은 후보 구현의 최종 측정에만 사용합니다.')
    suite,manifest=frozen_suite(args.suite,args.fixtures)
    if args.phase=='baseline':
        repo=Path(__file__).resolve().parents[2]
        for filename,expected in manifest['baseline_code'].items():
            if hashlib.sha256((repo / filename.replace('\\','/')).read_bytes()).hexdigest()!=expected:
                raise ValueError('동결 당시 코드와 다릅니다. 기준 측정을 덮어쓰지 마세요.')
    if args.json.exists():
        raise ValueError('기존 평가 결과를 덮어쓸 수 없습니다. 새 결과 경로를 지정하세요.')
    from ..ai.client import OllamaClient
    client=OllamaClient()
    health=client.health()
    if not health.embedding_ready or not health.generation_ready:
        print('로컬 생성 모델과 임베딩 모델을 먼저 준비하세요.',flush=True)
        return 2
    db=prepare(args.folder,suite,manifest['files'][args.suite+'.json'],client)
    try:
        profiles={name:asdict(profile) if (profile:=client.profile(name)) else None
                  for name in (client.gen_model,client.embed_model)}
        import httpx
        with httpx.Client(timeout=3,trust_env=False,follow_redirects=False) as http:
            response=http.get(client.base_url+'/api/tags');response.raise_for_status()
            installed=[{'name':m.get('name'),'digest':m.get('digest'),
                        'runner':m.get('details',{}).get('runner')}
                       for m in response.json().get('models',[])]
        record={'suite':args.suite,'phase':args.phase,'frozen_manifest':manifest,
                'models':profiles,'installed_models':installed,'seeds':list(range(args.repeat)),
                'created_at':datetime.now(timezone.utc).isoformat(),
                'code_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                              Path(__file__).resolve().parents[1].joinpath('search').glob('*.py')},
                'metrics':[],'rounds':[],'complete':False}
        args.json.parent.mkdir(parents=True,exist_ok=True)
        for seed in range(args.repeat):
            print(f'[{seed+1}/{args.repeat}회차] seed={seed}',flush=True)
            outcomes=[]
            for case in suite['cases']:
                client=project_client(db);client.seed=seed
                print(f"  {case['id']} … ",end='',flush=True)
                result=run_case(db,case,client);outcomes.append(result)
                print(f"{'통과' if result.passed else '실패'} {result.path} ({result.seconds}s)",flush=True)
            record['metrics'].append(rag_report.metrics(suite['cases'],outcomes))
            record['rounds'].append([asdict(o) for o in outcomes])
            args.json.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            rag_report._print_metrics(suite['cases'],outcomes)
        record['complete']=True
        args.json.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return 0 if all(not o['failures'] for run in record['rounds'] for o in run) else 1
    finally:
        db.close()


if __name__=='__main__':
    raise SystemExit(main())
