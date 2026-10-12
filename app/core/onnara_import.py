"""온나라 수집본을 노트북의 자료로 가져온다.

수집본은 수집 엔진(jobs/onnara_collect.py)이 남긴 폴더다 — manifest.json과
files/ 아래의 본문·붙임. 이 모듈은 **이미 내려받아 둔 폴더를 읽기만 한다.**
온나라에 접속하지 않고, 인증 정보를 다루지 않는다. 실제 기관 연결은 아직
검증하지 않았다(docs/온나라_연결프로필.md).

가져오면 세 가지가 일어난다.
  ① files/ 폴더를 자료원(kind='onnara')으로 등록하고 문서를 읽는다.
  ② manifest의 문서번호·상태·본문/붙임 관계를 문서에 잇는다(core/provenance.py).
     그 뒤로 인용에는 공식 출처가 함께 표시된다.
  ③ 읽은 문서를 이 노트북의 자료로 연결한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ..connectors.onnara import saide_package
from ..ingest import scanner
from ..ingest.hasher import sha256
from . import notebook, notebook_ingest, provenance
from .notebook_ingest import Ingested

MANIFEST = "manifest.json"
COLLECTION_DIR = "onnara"
PHASE_SCAN = "수집본을 확인하는 중"
PHASE_PACKAGE = "꾸러미의 파일을 확인하는 중"
FIDELITY_NOTES = {"rendered-text": "본문 {n}건은 화면 글자로 만든 PDF",
                  "extracted-text": "본문 {n}건은 추출한 글자",
                  "unstated": "본문 {n}건은 원본 여부 미상"}


def is_package(folder: str | Path) -> bool:
    """sAIde 꾸러미 폴더인가 — 수집본(manifest.json)이 아니라 아직 수집 전인 폴더."""
    folder = Path(folder)
    return saide_package.is_package(folder) and not (folder / MANIFEST).is_file()


def describe_package(folder: str | Path) -> str:
    """꾸러미를 가져오기 전에 보여 줄 한마디. 꾸러미가 아니면 이유를 말하며 거절한다."""
    connector = saide_package.SaidePackageConnector(folder)
    assets = [asset for items in connector.assets.values() for asset in items]
    parts = [f"sAIde 꾸러미 · 문서 {len(connector.records)}건 · 파일 {len(assets)}개"]
    return " · ".join(parts + _package_notes(connector.details))


def _package_notes(details: dict) -> list[str]:
    notes = []
    if details.get("author_identity") == "name":
        notes.append("작성자는 이름 기준(동명이인 구분 안 됨)")
    weak = details.get("id_basis", {}).get("list-row", 0)
    if weak:
        notes.append(f"문서 {weak}건은 문서 ID 없이 목록 행으로 식별")
    for key, text in FIDELITY_NOTES.items():
        count = details.get("body_fidelity", {}).get(key, 0)
        if count:
            notes.append(text.format(n=count))
    if details.get("body_truncated"):
        notes.append(f"본문 {details['body_truncated']}건은 일부만 담김")
    return notes


def collect_package(db, folder: str | Path,
                    on_progress: Callable[[str, str], None] = lambda phase, label: None,
                    should_stop: Callable[[], bool] = lambda: False) -> Path:
    """꾸러미를 프로젝트 안의 수집본으로 만든다. 꾸러미 폴더는 읽기만 한다.

    같은 꾸러미를 다시 가져오면 같은 수집본을 이어 쓴다(받은 파일은 다시 복사하지 않는다).
    """
    from ..jobs.onnara_collect import Collection  # jobs 묶음은 화면 쪽 모듈을 함께 싣는다
    connector = saide_package.SaidePackageConnector(folder)
    key = connector.run_id or provenance.collection_key(
        {"adapter": connector.details, "scope": str(connector.root)})[:16]
    target = db.path.parent / COLLECTION_DIR / key
    state = Collection(connector, connector.scope(), target).run(should_stop, on_progress)
    db.audit("onnara.package.collect", key,
             detail="; ".join(f"{name}={state['summary'].get(name)}"
                              for name in ("documents", "downloaded", "failed", "completeness")))
    if not (target / "files").is_dir():
        raise ValueError("꾸러미에서 가져올 수 있는 파일이 없습니다. 내려받기가 끝난 꾸러미인지 확인하세요.")
    return target


def read_manifest(folder: str | Path) -> tuple[Path, dict]:
    """수집 폴더와 그 manifest. 수집본이 아니면 이유를 말하며 거절한다."""
    root = Path(folder).resolve()
    path = root / MANIFEST
    if not path.is_file():
        raise ValueError("온나라 수집본 폴더가 아닙니다. manifest.json이 있는 수집 폴더를 고르세요.")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"manifest.json을 읽을 수 없습니다: {exc}") from exc
    if state.get("schema_version") != 1 or "identity" not in state or "assets" not in state:
        raise ValueError("이 프로그램이 아는 수집본 형식이 아닙니다(manifest 판이 다릅니다).")
    if not (root / "files").is_dir():
        raise ValueError("수집본에 내려받은 파일(files 폴더)이 없습니다.")
    return root, state


def describe(state: dict) -> str:
    """가져오기 전에 보여 줄 한마디 — 무엇을 얼마나 담은 수집본인가."""
    summary = state.get("summary", {})
    adapter = state["identity"].get("adapter", {})
    parts = [f"문서 {summary.get('documents', len(state.get('documents', {})))}건",
             f"파일 {summary.get('downloaded', 0)}/{summary.get('assets', len(state['assets']))}개 받음"]
    if summary.get("completeness") and summary["completeness"] != "complete":
        parts.append("일부만 수집됨" if summary["completeness"] == "partial" else "전체 수집 여부 미확인")
    if adapter.get("simulation") or summary.get("simulation"):
        parts.append("합성(모의) 수집본")
    elif not (adapter.get("live_verified") or summary.get("live_verified")):
        parts.append("실환경 검증 전 연결부")
    return " · ".join(parts + _package_notes(state.get("package") or {}))


def add_to_project(db, folder: str | Path,
                   should_stop: Callable[[], bool] = lambda: False) -> dict:
    """수집본(또는 sAIde 꾸러미)을 프로젝트의 자료 폴더로 등록한다 — 노트북을 거치지 않는다.

    여기서는 등록만 한다. 파일을 읽고 업무를 파악하는 일은 다른 자료 폴더와 똑같이
    분석(jobs/pipeline.py)이 하고, 분석이 파일을 읽은 뒤 `relink`가 공식 출처를 잇는다.
    """
    if is_package(folder):
        folder = collect_package(db, folder, should_stop=should_stop)
    root, state = read_manifest(folder)
    source_id = db.add_source(str(root / "files"), kind="onnara")
    # 문서 행이 아직 없으면 출처 기록만 남는다(unlinked). 분석 뒤 relink가 잇는다.
    linked = provenance.register_collection(db, state, root)
    db.audit("onnara.project.add", str(source_id),
             detail=f"records={linked['records']}; assets={linked['assets']}")
    return {"source_id": source_id, "root": root, "summary": describe(state), **linked}


def relink(db) -> int:
    """읽기를 마친 수집본 파일에 공식 출처를 잇는다. 새로 이어진 파일 수를 돌려준다.

    이을 것이 없으면 아무것도 하지 않는다 — 분석을 돌릴 때마다 불린다.
    """
    pending = "SELECT COUNT(*) FROM source_assets WHERE status='downloaded' AND doc_id IS NULL"
    before = db.con.execute(pending).fetchone()[0]
    if not before:
        return 0
    for source in db.sources():
        if source["kind"] != "onnara":
            continue
        try:
            root, state = read_manifest(Path(source["path"]).parent)
        except ValueError:      # 수집본 폴더를 옮겼거나 지웠다. 출처 기록은 그대로 둔다.
            continue
        provenance.register_collection(db, state, root)
    return before - db.con.execute(pending).fetchone()[0]


def import_collection(db, notebook_id: int, folder: str | Path, client=None,
                      on_phase: Callable[[str, str], None] = lambda name, phase: None,
                      should_stop: Callable[[], bool] = lambda: False) -> list[Ingested]:
    """수집본의 문서를 읽고 공식 출처를 이어 노트북에 연결한다.

    sAIde 꾸러미 폴더를 받으면 먼저 프로젝트 안에 수집본을 만든다.
    """
    notebook.require(db, notebook_id)
    if is_package(folder):
        on_phase(Path(folder).name, PHASE_PACKAGE)
        folder = collect_package(db, folder, should_stop=should_stop)
        if should_stop():
            return []
    root, state = read_manifest(folder)
    files = root / "files"
    on_phase(root.name, PHASE_SCAN)
    source_id = db.add_source(str(files), kind="onnara")
    scanner.scan_source(db, source_id, files, should_stop=should_stop)

    results: list[Ingested] = []
    rows = db.con.execute(
        "SELECT id, path, filename, hash, parse_status FROM documents "
        "WHERE source_id=? AND missing_since IS NULL AND parse_status != 'skipped' ORDER BY id",
        (source_id,)).fetchall()
    for row in rows:
        if should_stop():
            break
        outcome = Ingested(row["path"], row["filename"], row["id"])
        if row["hash"] is None:
            digest, error = sha256(row["path"])
            if digest is None:
                outcome.status, outcome.error = "rejected", error or "파일을 읽을 수 없습니다."
                results.append(outcome)
                continue
            db.update_document(row["id"], hash=digest)
        if row["parse_status"] in ("ok", "partial"):
            outcome.status = row["parse_status"]
            notebook_ingest.write_chunks(db, row["id"], client, should_stop)
        else:
            outcome = notebook_ingest.reprocess(
                db, row["id"], client, lambda phase, name=row["filename"]: on_phase(name, phase), should_stop)
        results.append(outcome)

    # 문서 행이 생긴 뒤에 등록해야 공식 출처가 파일에 이어진다.
    linked = provenance.register_collection(db, state, root)
    usable = [r.doc_id for r in results if r.usable]
    if usable:
        notebook.add_sources(db, notebook_id, usable, origin="onnara")
    db.audit("notebook.onnara.import", str(notebook_id),
             detail=f"documents={len(usable)}; linked={linked['linked']}; unlinked={linked['unlinked']}")
    return results
