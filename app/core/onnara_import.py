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

from ..ingest import scanner
from ..ingest.hasher import sha256
from . import notebook, notebook_ingest, provenance
from .notebook_ingest import Ingested

MANIFEST = "manifest.json"
PHASE_SCAN = "수집본을 확인하는 중"


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
    return " · ".join(parts)


def import_collection(db, notebook_id: int, folder: str | Path, client=None,
                      on_phase: Callable[[str, str], None] = lambda name, phase: None,
                      should_stop: Callable[[], bool] = lambda: False) -> list[Ingested]:
    """수집본의 문서를 읽고 공식 출처를 이어 노트북에 연결한다."""
    notebook.require(db, notebook_id)
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
