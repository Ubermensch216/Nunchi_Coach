"""Read-only single-file HWPX audit against independent XML text/cell checks.

Outputs a local report and isolated upload-only project in a new directory.
Does not call a model, scan the parent folder or copy the source into the repo.
Value containment checks cannot establish occurrence counts or visual fidelity.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import zipfile

from lxml import etree

from ..core.coverage import snapshot
from ..core.notebook_ingest import write_parse, write_date, write_chunks
from ..db import Database
from ..ingest.parsers import parse
from ..ingest.parsers.base import MAX_CELLS_PER_SHEET
from .console import force_utf8


def _local(node):
    return node.tag.rsplit('}', 1)[-1] if isinstance(node.tag, str) else ''


def _norm(text):
    return ''.join(text.split())


def _cell_text(cell):
    # Exclude nested table text: it belongs to that table's own audit.
    parts = []
    def visit(node):
        if _local(node) == 'tbl':
            return
        if _local(node) == 't':
            parts.append(''.join(node.itertext()))
            return
        for child in node:
            visit(child)
    visit(cell)
    return _norm(''.join(parts))


def inspect_xml(source, result):
    body = _norm(result.text)
    runs = matched_runs = cells = placements = matched_placements = untested = 0
    missing_runs, missing_cells = [], []
    with zipfile.ZipFile(source) as archive:
        names = sorted((n for n in archive.namelist() if re.fullmatch(r'contents/section\d+\.xml', n, re.I)),
                       key=lambda n: int(re.search(r'section(\d+)', n, re.I).group(1)))
        for name in names:
            root = etree.fromstring(archive.read(name), etree.XMLParser(resolve_entities=False, no_network=True))
            for index, node in enumerate(root.iter()):
                if _local(node) == 't':
                    value = _norm(''.join(node.itertext()))
                    if value:
                        runs += 1
                        if value in body:
                            matched_runs += 1
                        else:
                            missing_runs.append({'part': name, 'element_index': index})
            tables = [node for node in root.iter() if _local(node) == 'tbl']
            for number, table in enumerate(tables, 1):
                rows = {s.meta['rows'][0]: _norm(s.text) for s in result.sections
                        if s.kind == 'table' and s.meta.get('table') == number and s.locator.startswith(name + ' ·')}
                column_ids = set()
                direct_cells = []
                for row_index, row in enumerate(child for child in table if _local(child) == 'tr'):
                    for col_index, cell in enumerate(child for child in row if _local(child) == 'tc'):
                        address = next((n for n in cell if _local(n) == 'cellAddr'), None)
                        span = next((n for n in cell if _local(n) == 'cellSpan'), None)
                        r = int(address.get('rowAddr', row_index)) if address is not None else row_index
                        c = int(address.get('colAddr', col_index)) if address is not None else col_index
                        w = max(1, int(span.get('colSpan', 1))) if span is not None else 1
                        h = max(1, int(span.get('rowSpan', 1))) if span is not None else 1
                        value = _cell_text(cell)
                        cells += 1
                        if w * h > MAX_CELLS_PER_SHEET or len(direct_cells) + w * h > MAX_CELLS_PER_SHEET:
                            untested += 1
                            continue
                        for down in range(h):
                            for across in range(w):
                                direct_cells.append((r + down, c + across, value))
                                column_ids.add(c + across)
                for r, c, value in direct_cells:
                    if not value:
                        continue
                    placements += 1
                    needle = _norm(f'{c + 1}열: ') + value if len(column_ids) > 1 else value
                    if needle in rows.get(r + 1, ''):
                        matched_placements += 1
                    elif len(missing_cells) < 100:
                        missing_cells.append({'part': name, 'table': number, 'row': r + 1, 'column': c + 1})
    return {'nonempty_text_runs': runs, 'matched_text_run_values': matched_runs,
            'missing_text_run_locators': missing_runs[:100],
            'physical_table_cells': cells, 'nonempty_cell_placements': placements,
            'matched_cell_placements': matched_placements, 'untested_cells': untested,
            'missing_cell_locator_samples': missing_cells,
            'check_type': 'whitespace_normalized_value_containment_at_table_row_column',
            'limitations': ['duplicate_text_occurrence_counts_not_verified', 'visual_layout_not_verified']}


def run(source: Path, output: Path):
    source, output = source.resolve(strict=True), output.resolve()
    if source.suffix.lower() != '.hwpx':
        raise ValueError('This independent XML audit requires an HWPX file; HWP is a different container.')
    if output.is_relative_to(source.parent) or source.is_relative_to(output):
        raise ValueError('Use a new output directory outside the source folder.')
    before_stat = source.stat()
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    result = parse(source)
    checks = inspect_xml(source, result)
    output.mkdir(parents=True, exist_ok=False)
    db = Database(output / 'project.db')
    try:
        db.init()
        # Upload sources are excluded from automatic directory scanning/discovery.
        sid = db.add_source(source.parent, kind='upload')
        doc = db.upsert_document(sid, {'path': str(source), 'filename': source.name, 'ext': '.hwpx',
                                    'hash': before_hash, 'fs_mtime': before_stat.st_mtime,
                                    'size': before_stat.st_size})
        write_parse(db, doc, source.name, str(source), result)
        write_date(db, doc)
        write_chunks(db, doc, None)
        row = db.document(doc)
        chunks = [dict(r) for r in db.con.execute('SELECT locator,text FROM chunks WHERE doc_id=? ORDER BY ordinal', (doc,))]
        combined = _norm('\n'.join(c['text'] for c in chunks))
        preserved = all(_norm(s.text) in combined for s in result.sections if s.text)
        date = {k: row[k] for k in ('eff_date', 'eff_date_kind', 'eff_year', 'eff_precision')}
        db.close()
        # Reopen after all writers close: stored scope must survive process/session restart.
        reopened = snapshot(db, include_documents=True)
        stored = reopened['documents'][0]['extraction_coverage']
        after_stat = source.stat()
        after_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        report = {'source': source.name, 'source_sha256': before_hash, 'source_bytes': before_stat.st_size,
                  'source_bytes_unchanged': before_hash == after_hash,
                  'source_mtime_unchanged': before_stat.st_mtime_ns == after_stat.st_mtime_ns,
                  'status': result.status, 'parser_version': result.parser_version,
                  'extracted_characters': result.char_count, 'extracted_sections': len(result.sections),
                  'metadata': asdict(result.meta), 'document_date': date, 'coverage': result.coverage,
                  'scope_persisted_after_reopen': stored == result.coverage, 'xml_checks': checks,
                  'parent_chunks': len(chunks), 'section_values_preserved_in_chunks': preserved,
                  'model_calls': 0, 'scope': 'one_real_hwpx_structural_and_storage_validation',
                  'limitations': ['no_real_hwp_validation', 'no_business_precision_recall_estimate',
                                  'no_llm_answer_accuracy_test', 'no_visual_rendering_validation']}
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return report
    finally:
        db.close()


def main(argv=None):
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run(args.source, args.output)
    except (ValueError, OSError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    checks = report['xml_checks']
    return 0 if (report['status'] == 'ok' and report['source_bytes_unchanged']
                 and report['scope_persisted_after_reopen'] and report['section_values_preserved_in_chunks']
                 and checks['matched_text_run_values'] == checks['nonempty_text_runs']
                 and checks['matched_cell_placements'] == checks['nonempty_cell_placements']
                 and not checks['untested_cells']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
