"""Generate and collect synthetic On-nara documents; no live institution access."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from ..connectors.onnara import FixtureConnector, SearchScope
from ..core.provenance import register_collection
from ..jobs.onnara_collect import Collection


def make_catalog(directory: str | Path, count: int = 30) -> Path:
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    catalog = root / "catalog.json"
    if catalog.exists():
        raise ValueError("합성 자료가 이미 있습니다. 새 폴더를 지정해 주세요.")
    files = root / "files"
    files.mkdir(exist_ok=True)
    documents = []
    for index in range(1, count + 1):
        doc_id = f"SYN-{index:04d}"
        # Include a namesake with a different stable author ID.
        author = "author-001" if index <= count - 3 else "author-002"
        body = files / f"{doc_id}_2025_실적보고.odt"
        content = f'''<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:text>
<text:h>2025년 정기 실적 보고</text:h><text:p>작성일: 2025-09-01. 작성자: 합성담당자.</text:p>
<text:p>절차: 집계 자료 준비 → 실적표 작성 → 검토 후 제출. 제출기한: 2025-09-30.</text:p>
<table:table><table:table-row><table:table-cell><text:p>사업</text:p></table:table-cell><table:table-cell><text:p>예산(백만원)</text:p></table:table-cell></table:table-row>
<table:table-row><table:table-cell><text:p>시범사업 {index}</text:p></table:table-cell><table:table-cell><text:p>820</text:p></table:table-cell></table:table-row></table:table>
</office:text></office:body></office:document-content>'''
        with zipfile.ZipFile(body, "w") as archive:
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            archive.writestr("content.xml", content)
        assets = []
        for path, role in ((body, "body"), (files / f"{doc_id}_집계.csv", "attachment"),
                           (files / f"{doc_id}_제출안내.txt", "attachment")):
            if role == "attachment":
                path.write_text("사업,금액(백만원)\n시범사업,820\n" if path.suffix == ".csv"
                                else "실적 보고는 집계 자료와 결과표를 함께 준비합니다.", encoding="utf-8")
            assets.append({"asset_id": path.name, "filename": path.name, "role": role,
                           "media_type": "application/vnd.oasis.opendocument.text" if role == "body" else "text/plain",
                           "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "path": path.relative_to(root).as_posix()})
        documents.append({"document_id": doc_id, "title": f"실적보고 {index}",
                          "author_id": author, "author_name": "합성담당자", "department": "합성부서",
                          "authored_at": "2025-09-01", "status": "approved",
                          "document_number": f"합성부서-{index}", "assets": assets})
    catalog.write_text(json.dumps({"synthetic": True, "institution": "합성기관",
                                   "system_id": "synthetic-onnara", "documents": documents},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    return catalog


def index_collection(state, collection_root: Path, project_dir: Path):
    """Connect downloaded assets to the existing local parser and FTS pipeline."""
    from ..db import Database
    from ..jobs.pipeline import Pipeline
    db = Database(project_dir / "project.db")
    db.init()
    source_id = db.add_source(str((collection_root / "files").resolve()), kind="onnara")
    pipeline = Pipeline(db.path)
    pipeline._scan(db)
    pipeline._hash(db)
    pipeline._parse(db)
    pipeline._date(db)
    pipeline._chunks(db)
    parsed = db.con.execute("SELECT COUNT(*) FROM documents WHERE source_id=? AND parse_status IN ('ok','partial')",
                            (source_id,)).fetchone()[0]
    failed = db.con.execute("SELECT COUNT(*) FROM documents WHERE source_id=? AND parse_status NOT IN ('ok','partial')",
                            (source_id,)).fetchone()[0]
    # Official IDs, status and body/attachment relations follow each file into the project.
    linked = register_collection(db, state, collection_root)
    db.set_meta("onnara_manifest", str((collection_root / "manifest.json").resolve()))
    db.audit("onnara.fixture.index", str(source_id), detail=f"parsed={parsed}; failed={failed}")
    db.close()
    return {"parsed": parsed, "failed": failed, "records": linked["records"],
            "linked": linked["linked"], "unlinked": linked["unlinked"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="온나라 합성 수집 시험 (실환경 연결 아님)")
    parser.add_argument("--make-fixture", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--author-id", default="author-001")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2025-12-31")
    args = parser.parse_args(argv)
    try:
        catalog = make_catalog(args.make_fixture) if args.make_fixture else args.catalog
        if catalog is None:
            parser.error("--catalog 또는 --make-fixture가 필요합니다.")
        connector = FixtureConnector(catalog)
        state = Collection(connector, SearchScope((args.author_id,), args.start, args.end), args.output).run()
        result = {"status": state["status"], **state["summary"]}
        if args.project:
            result["index"] = index_collection(state, args.output.resolve(), args.project.resolve())
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result["status"] == "finished" and result["completeness"] == "complete" else 1
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
