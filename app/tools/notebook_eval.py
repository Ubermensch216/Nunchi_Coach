"""동결한 합성 시나리오를 notebook.query로 평가한다. 기존 프로젝트는 사용하지 않는다.

python -m app.tools.notebook_eval --suite dev --phase candidate --folder .test_runs/notebook-eval --json .test_runs/notebook-eval.json
검색 확장 on/off, 후속 질문, 선택 변경, 전체 정리와 저장된 인용을 함께 검사한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..ai.client import OllamaClient
from ..core import notebook
from . import rag_quality, rag_report

FIXTURES = Path(__file__).resolve().parents[2] / 'tests/fixtures/notebook_rag_2026_10_09'


def judge_turn(db, message_id, answer, spec, snapshot):
    """화면 답변뿐 아니라 실제 저장된 선택 범위·원문 인용도 채점한다."""
    outcome = rag_report.Outcome(spec.get('id', ''), answer.question, spec['expect'],
                                answer.withheld, 0, [c.filename for c in answer.citations],
                                answer.text, answer.error)
    outcome.path = answer.generation.get('withheld') or answer.generation.get('rule_answer') or 'model'
    if answer.generation.get('mode') == 'search_only':
        outcome.error = '모델·의미 색인이 준비되지 않아 평가할 수 없습니다.'
    failures = rag_report._judge(spec, outcome)
    row = db.con.execute('SELECT * FROM notebook_messages WHERE id=?', (message_id,)).fetchone()
    saved = json.loads(row['citations'])
    if json.loads(row['source_snapshot']) != snapshot:
        failures.append('저장한 선택 범위가 질문 시작 범위와 다르다')
    if row['answer'] != answer.text or bool(row['withheld']) != answer.withheld:
        failures.append('화면 답변과 저장 답변이 다르다')
    for citation in saved:
        key = str(citation['doc_id'])
        original = db.con.execute('SELECT text FROM chunks WHERE id=? AND doc_id=?',
                                  (citation['chunk_id'], citation['doc_id'])).fetchone()
        if (key not in snapshot or citation.get('version_hash') != snapshot.get(key)
                or not original or citation.get('exact_quote') != original[0]):
            failures.append('선택 범위·판본·원문 인용 불일치')
    if spec.get('mode') and row['mode'] != spec['mode']:
        failures.append('요청한 처리 방식이 저장되지 않았다')
    if spec.get('must_rewrite') and not row['standalone_question']:
        failures.append('후속 질문 독립화 실패')
    if spec.get('must_not_rewrite') and row['standalone_question']:
        failures.append('선택 변경 뒤 이전 문맥을 사용했다')
    return outcome, failures, row['standalone_question']


def run_scenario(db, scenario, client, expansion):
    mapping = {row['filename']: row['id'] for row in db.con.execute('SELECT id,filename FROM documents')}
    book = notebook.create(db, scenario['id'])
    notebook.add_sources(db, book, [mapping[name] for name in scenario['sources']])
    db.set_meta('notebook_query_expansion', '1' if expansion else '0')
    records = []
    for index, spec in enumerate(scenario['turns']):
        if 'select' in spec:
            # 필요하면 새 원문을 연결한 뒤 선택을 다시 정한다.
            notebook.add_sources(db, book, [mapping[name] for name in spec['select']])
            notebook.select_all(db, book, False)
            for name in spec['select']:
                notebook.select(db, book, mapping[name], True)
        snapshot = notebook.selected_snapshot(db, book)
        started = time.monotonic()
        try:
            message, answer = notebook.query(db, book, spec['question'], client,
                                              mode=spec.get('mode', ''))
            outcome, failures, rewritten = judge_turn(db, message, answer, spec, snapshot)
            expected = {mapping[name] for name in spec.get('must_cite_any', []) if name in mapping}
            retrieved = set(answer.generation.get('retrieval', {}).get('selected_doc_ids', []))
            if not failures:
                failure_kind = ''
            elif any('범위' in f or '판본' in f for f in failures):
                failure_kind = 'scope_or_version'
            elif answer.error:
                failure_kind = 'runtime_error'
            elif expected and not (expected & (retrieved | {c.doc_id for c in answer.citations})):
                failure_kind = 'retrieval_miss'
            elif answer.withheld:
                failure_kind = 'over_abstention'
            else:
                failure_kind = 'answer_or_evidence_mismatch'
            record = {'turn': index, 'question': spec['question'], 'answer': answer.text,
                      'withheld': answer.withheld, 'path': outcome.path,
                      'citations': [asdict(c) for c in answer.citations],
                      'generation': answer.generation, 'standalone_question': rewritten,
                      'source_snapshot': snapshot, 'failures': failures, 'failure_kind': failure_kind}
        except Exception as exc:
            record = {'turn': index, 'failures': [f'실행 오류: {type(exc).__name__}: {exc}']}
        record['seconds'] = round(time.monotonic() - started, 3)
        records.append(record)
    return {'id': scenario['id'], 'category': scenario['category'], 'expansion': expansion,
            'passed': all(not turn['failures'] for turn in records), 'turns': records}


def summarize(records):
    turns = [turn for scenario in records for turn in scenario['turns']]
    return {'scenarios': len(records), 'passed_scenarios': sum(r['passed'] for r in records),
            'turns': len(turns), 'passed_turns': sum(not t['failures'] for t in turns),
            'seconds': round(sum(t['seconds'] for t in turns), 3)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=['dev', 'holdout'], required=True)
    parser.add_argument('--phase', choices=['baseline', 'candidate'], required=True)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--json', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, default=FIXTURES)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--expansion', choices=['both', 'on', 'off'], default='both')
    parser.add_argument('--limit', type=int, default=0, help='앞의 N개 시나리오만 측정; 결과에 부분 실행으로 표시')
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 10 or args.limit < 0 or (args.suite == 'holdout' and args.phase == 'baseline'):
        parser.error('반복은 1~10회, limit은 0 이상, holdout은 candidate만 가능합니다.')
    rag_report._force_utf8()
    suite, manifest = rag_quality.frozen_suite(args.suite, args.fixtures)
    repo = Path(__file__).resolve().parents[2]
    if args.phase == 'baseline':
        for filename, expected in manifest['baseline_code'].items():
            if hashlib.sha256((repo / filename).read_bytes()).hexdigest() != expected:
                raise ValueError('동결 당시 코드와 다릅니다. 기준선 스냅샷에서 실행하세요.')
    if args.json.exists():
        raise ValueError('기존 결과를 덮어쓸 수 없습니다.')
    client = OllamaClient()
    health = client.health()
    if not health.embedding_ready or not health.generation_ready:
        print('로컬 생성·임베딩 모델을 준비하세요.')
        return 2
    # prepare는 생성한 평가 DB만 재사용하고 기존 프로젝트/변경된 합성 원문을 거절한다.
    db = rag_quality.prepare(args.folder, suite, manifest['files'][args.suite + '.json'], client)
    try:
        import httpx
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as http:
            response = http.get(client.base_url + '/api/tags')
            response.raise_for_status()
            installed = [{'name': m.get('name'), 'digest': m.get('digest')} for m in response.json()['models']]
        profiles = {name: asdict(profile) if (profile := client.profile(name)) else None
                    for name in (client.gen_model, client.embed_model)}
        scenarios = suite['scenarios'][:args.limit] if args.limit else suite['scenarios']
        record = {'suite': args.suite, 'phase': args.phase, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'reference_date': suite['reference_date'], 'frozen_manifest': manifest,
                  'models': profiles, 'installed_models': installed, 'seeds': list(range(args.repeat)),
                  'partial': len(scenarios) != len(suite['scenarios']), 'scenario_count': len(scenarios),
                  'code_sha256': {str(p.relative_to(repo)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for folder in ('app/search', 'app/core', 'app/ai', 'app/ingest/parsers')
                                  for p in (repo / folder).glob('*.py')},
                  'rounds': [], 'complete': False}
        args.json.parent.mkdir(parents=True, exist_ok=True)
        with args.json.open('x', encoding='utf-8') as out:
            json.dump(record, out, ensure_ascii=False, indent=2)
        for seed in range(args.repeat):
            for enabled in ([False, True] if args.expansion == 'both' else [args.expansion == 'on']):
                results = []
                for scenario in scenarios:
                    client = OllamaClient()
                    client.seed = seed
                    result = run_scenario(db, scenario, client, enabled)
                    results.append(result)
                    print(f"seed={seed} expansion={enabled} {scenario['id']} {'PASS' if result['passed'] else 'FAIL'}", flush=True)
                    # 중단되더라도 완료한 시나리오는 남긴다.
                    checkpoint = {**record, 'in_progress': {'seed': seed, 'expansion': enabled, 'scenarios': results}}
                    args.json.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding='utf-8')
                record['rounds'].append({'seed': seed, 'expansion': enabled,
                                         'summary': summarize(results), 'scenarios': results})
        record['complete'] = True
        args.json.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps([r['summary'] for r in record['rounds']], ensure_ascii=False))
        return 0 if all(s['passed'] for r in record['rounds'] for s in r['scenarios']) else 1
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
