"""온나라 공식 출처 — 수집 manifest를 프로젝트 문서에 잇는다.

수집 엔진(jobs/onnara_collect.py)은 프로젝트 DB를 모른다. 여기서 manifest의
문서 ID·상태·본문/붙임 관계를 DB에 옮기고, 내려받은 파일을 이미 분석된
문서 행에 연결한다. 그래야 답변의 근거가 '어느 폴더의 어느 파일'을 넘어
'어느 공문의 본문/붙임인가'를 말할 수 있다.

원칙 (구축계획서 §4.5.5, §5.2)
  · 키는 기관+시스템+문서 ID다. 경로·파일명으로 문서를 식별하지 않는다.
  · 제공되지 않은 값은 비워 둔다. 상태·시점을 추정해 채우지 않는다.
  · 다시 수집했을 때 목록에서 사라진 문서의 기록을 지우지 않는다 —
    권한 변경인지 원문 삭제인지 여기서는 알 수 없다.
  · 결재 상태는 출처 정보일 뿐이다. 현재 효력이나 사실 여부를 뜻하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

# 연결부가 준 상태값의 표시명. 모르는 값은 원문 그대로 보여 준다 —
# 기관마다 상태 체계가 다르고, 임의로 '결재완료'에 끼워 맞추면 안 된다.
STATUS_LABELS = {
    "approved": "결재완료",
    "registered": "등록",
    "sent": "발송",
    "draft": "초안",
    "rejected": "반려",
    "unknown": "상태 미상",
    "": "상태 미상",
}
ROLE_LABELS = {"body": "본문", "attachment": "붙임"}
# 온나라가 준 파일 그대로(original)는 표시하지 않는다 — 그것이 기본이다.
FIDELITY_LABELS = {"rendered-text": "화면 글자로 만든 PDF", "extracted-text": "추출한 글자"}


def collection_key(identity: dict) -> str:
    """연결부+검색 범위의 지문. 같은 범위를 다시 수집하면 같은 실행으로 갱신된다."""
    text = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def register_collection(db, state: dict, collection_root: str | Path) -> dict:
    """수집 결과를 프로젝트 DB에 등록하고 내려받은 파일을 문서 행에 잇는다.

    문서 행은 이미 스캔·해시된 뒤여야 연결된다. 아직 분석 전이면 출처 기록만
    남고 `unlinked`로 센다 — 다음 등록에서 다시 이어진다(멱등).
    """
    root = Path(collection_root).resolve()
    adapter = state["identity"]["adapter"]
    summary = state.get("summary", {})
    result = {"records": 0, "assets": 0, "linked": 0, "unlinked": 0, "changed": 0}

    with db.transaction():
        con = db.con
        key = collection_key(state["identity"])
        con.execute(
            "INSERT INTO collection_runs(collection_key, adapter, institution, system_id, scope,"
            " simulation, live_verified, status, completeness, expected_documents, documents,"
            " assets, downloaded, failed, started_at, collected_at, author_identity)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(collection_key) DO UPDATE SET"
            " simulation=excluded.simulation, live_verified=excluded.live_verified,"
            " status=excluded.status, completeness=excluded.completeness,"
            " expected_documents=excluded.expected_documents, documents=excluded.documents,"
            " assets=excluded.assets, downloaded=excluded.downloaded, failed=excluded.failed,"
            " collected_at=excluded.collected_at, registered_at=datetime('now'),"
            " author_identity=excluded.author_identity",
            (key, adapter["adapter"], adapter["institution"], adapter["system_id"],
             json.dumps(state["identity"]["scope"], ensure_ascii=False),
             int(bool(adapter.get("simulation"))), int(bool(adapter.get("live_verified"))),
             state.get("status"), summary.get("completeness"), summary.get("expected_documents"),
             summary.get("documents", 0), summary.get("assets", 0),
             summary.get("downloaded", 0), summary.get("failed", 0),
             state.get("started_at"), state.get("updated_at"),
             str((state.get("package") or {}).get("author_identity") or "")),
        )
        run_id = con.execute(
            "SELECT id FROM collection_runs WHERE collection_key=?", (key,)
        ).fetchone()["id"]

        record_ids: dict[str, int] = {}
        for document_id, record in state.get("documents", {}).items():
            origin = (adapter["institution"], adapter["system_id"], document_id)
            old = con.execute(
                "SELECT id, status FROM source_records"
                " WHERE institution=? AND system_id=? AND document_id=?", origin,
            ).fetchone()
            status = record.get("status") or "unknown"
            fields = (run_id, record.get("title", ""), record.get("document_number", ""),
                      record.get("author_id", ""), record.get("author_name", ""),
                      record.get("department", ""), record.get("author_role", ""), status,
                      record.get("authored_at"), record.get("registered_at"),
                      record.get("approved_at"), record.get("id_basis") or "",
                      record.get("task_card") or "")
            if old is None:
                cursor = con.execute(
                    "INSERT INTO source_records(run_id, title, document_number, author_id,"
                    " author_name, department, author_role, status, authored_at, registered_at,"
                    " approved_at, id_basis, task_card, institution, system_id, document_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*fields, *origin),
                )
                record_ids[document_id] = cursor.lastrowid
            else:
                con.execute(
                    "UPDATE source_records SET run_id=?, title=?, document_number=?, author_id=?,"
                    " author_name=?, department=?, author_role=?, status=?, authored_at=?,"
                    " registered_at=?, approved_at=?, id_basis=?, task_card=?,"
                    " last_collected=datetime('now') WHERE id=?",
                    (*fields, old["id"]),
                )
                record_ids[document_id] = old["id"]
                if old["status"] != status:
                    # 상태가 바뀐 사실 자체가 인계 정보다(§5.2). 본문은 남기지 않는다.
                    result["changed"] += 1
                    db.audit("onnara.record.status", document_id,
                             detail=f"{old['status']} → {status}")
            result["records"] += 1

        for entry in state.get("assets", {}).values():
            record_id = record_ids.get(entry["document_id"])
            if record_id is None:
                continue
            asset = entry["asset"]
            downloaded = entry.get("status") == "downloaded"
            digest = entry.get("sha256") if downloaded else asset.get("sha256")
            doc_id = _linked_document(con, root, entry) if downloaded else None
            con.execute(
                "INSERT INTO source_assets(record_id, asset_id, role, filename, media_type,"
                " sha256, size, status, error, downloaded_at, doc_id, fidelity)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(record_id, asset_id) DO UPDATE SET"
                " role=excluded.role, filename=excluded.filename, fidelity=excluded.fidelity,"
                " media_type=excluded.media_type, sha256=excluded.sha256, size=excluded.size,"
                " status=excluded.status, error=excluded.error,"
                " downloaded_at=excluded.downloaded_at,"
                " doc_id=COALESCE(excluded.doc_id, source_assets.doc_id)",
                (record_id, asset["asset_id"], asset["role"], asset.get("filename", ""),
                 asset.get("media_type", ""), digest,
                 entry.get("size") if downloaded else asset.get("size"),
                 entry.get("status", "pending"), entry.get("error"),
                 entry.get("downloaded_at"), doc_id, asset.get("fidelity") or ""),
            )
            result["assets"] += 1
            if doc_id is not None:
                result["linked"] += 1
            elif downloaded:
                result["unlinked"] += 1

    db.audit("onnara.register", key[:12],
             detail="; ".join(f"{name}={value}" for name, value in result.items()))
    return result


def _linked_document(con, root: Path, entry: dict) -> int | None:
    """내려받은 파일에 해당하는 문서 행. 경로가 먼저고, 없으면 같은 내용의 수집본."""
    path = str(root / Path(entry["path"]))
    row = con.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
    if row is None and entry.get("sha256"):
        # 수집 폴더를 옮긴 뒤 다시 등록한 경우 — 온나라 자료원 안에서만 찾는다.
        # 개인 폴더의 같은 내용 파일은 '같은 내용'으로 따로 표시한다(official_sources).
        row = con.execute(
            "SELECT d.id FROM documents d JOIN sources s ON s.id = d.source_id"
            " WHERE d.hash=? AND s.kind='onnara' AND d.filename LIKE ?"
            " ORDER BY d.id LIMIT 1",
            (entry["sha256"], "%" + Path(entry["path"]).name),
        ).fetchone()
    return row["id"] if row else None


_SELECT = (
    "SELECT a.id AS asset_row, a.doc_id AS linked_doc_id, a.sha256,"
    " a.role, a.filename AS asset_filename, a.asset_id, a.fidelity,"
    " r.id_basis, c.author_identity,"
    " r.document_id, r.title, r.document_number, r.author_name, r.department,"
    " r.author_role, r.status, r.authored_at, r.registered_at, r.approved_at,"
    " r.institution, r.system_id, c.simulation, c.live_verified, c.collected_at"
    " FROM source_assets a JOIN source_records r ON r.id = a.record_id"
    " JOIN collection_runs c ON c.id = r.run_id"
)


def official_sources(db, doc_ids: Sequence[int]) -> dict[int, dict]:
    """문서별 온나라 출처. 출처가 없는 문서는 결과에 넣지 않는다.

    수집본 자체면 `same_content`가 False다. 개인 폴더의 파일이 수집한 공식
    파일과 해시까지 같으면 True로 돌려준다 — 두 출처 연결을 모두 보존하되
    (§4.5.5) 개인 폴더 파일을 수집본이라고 부르지는 않는다. 내용이 조금이라도
    다르면 연결하지 않는다. 그건 버전 후보이지 같은 문서가 아니다.
    """
    ids = [int(i) for i in doc_ids]
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    out: dict[int, dict] = {}
    for row in db.con.execute(
        f"{_SELECT} WHERE a.doc_id IN ({marks}) ORDER BY a.id", ids
    ).fetchall():
        out.setdefault(row["linked_doc_id"], {**dict(row), "same_content": False})

    missing = [i for i in ids if i not in out]
    if missing:
        marks = ", ".join("?" * len(missing))
        for row in db.con.execute(
            f"SELECT d.id AS local_doc_id, x.* FROM documents d JOIN ({_SELECT}"
            f" WHERE a.status='downloaded') x ON x.sha256 = d.hash"
            f" WHERE d.id IN ({marks}) ORDER BY x.asset_row",
            missing,
        ).fetchall():
            out.setdefault(row["local_doc_id"], {**dict(row), "same_content": True})
    return out


def describe(official: dict | None) -> str:
    """근거 옆에 붙일 한 줄. `온나라(합성) · 합성부서-1 · 결재완료 · 본문`.

    아는 것만 적는다. 합성 자료는 반드시 합성이라고 밝힌다 — 모의 수집
    결과가 실제 기관 문서처럼 보이면 안 된다.
    """
    if not official:
        return ""
    origin = "온나라(합성)" if official.get("simulation") else "온나라"
    if official.get("same_content"):
        origin += " 수집본과 같은 내용"
    parts = [origin]
    if official.get("document_number"):
        parts.append(official["document_number"])
    status = official.get("status") or ""
    parts.append(STATUS_LABELS.get(status, status))
    role = official.get("role") or ""
    label = ROLE_LABELS.get(role, role)
    # 원본이 아닌 파일을 원본처럼 보이게 두지 않는다. 밝히지 않은 값(빈 값)은 덧붙이지 않는다.
    fidelity = FIDELITY_LABELS.get(official.get("fidelity") or "")
    parts.append(f"{label}({fidelity})" if label and fidelity else label or fidelity or "")
    if official.get("id_basis") == "list-row":
        parts.append("문서 ID 미확인")
    return " · ".join(part for part in parts if part)
