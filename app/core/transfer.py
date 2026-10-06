"""복원본·인계본의 이동과 원문 재연결 (구축계획서 C08, §5.3).

후임자의 PC는 전임자의 PC가 아니다. 드라이브 글자도, 폴더 위치도, 프로젝트
DB의 문서 번호도 다르다. 그래도 넘겨받은 사람이 **근거·사람의 수정·수행
이력**을 그대로 이어 볼 수 있어야 한다.

묶음(ZIP)에는 세 가지만 들어간다.
    manifest.json   형식·종류·기준 시각·지문·파일별 해시
    snapshot.json   스냅샷 본체 (C06)
    report.md       사람이 읽을 글

원문은 넣지 않는다. 받는 쪽이 자기 자료 폴더를 지정하면 해시와 자료원 안의
위치로 다시 잇는다. 묶음에는 인용문이 들어 있으므로 공개 파일로 다루면 안 된다.
API 키·절대 경로·진단 로그는 싣지 않는다.

가져올 때 지키는 것
  · 묶음을 믿지 않는다. 파일 이름·크기·형식 판·해시를 먼저 확인하고, 하나라도
    어긋나면 아무것도 쓰지 않는다. 압축을 폴더에 풀지 않는다 — 필요한 세
    파일만 메모리로 읽는다.
  · 묶음의 글은 자료일 뿐이다. 그 안의 문장을 명령으로 실행하지 않는다.
  · 받는 쪽에서 사람이 이미 고치거나 수행한 것은 덮지 않는다.
  · 원문을 못 찾아도 인용문은 읽을 수 있게 둔다. 찾았는지 못 찾았는지를
    숨기지 않는다.
  · 이름이 같아도 내용(해시)이 다르면 같은 원문이 아니다. 잇되 '원문이
    바뀌었다'고 표시한다.
  · 같은 내용의 파일이 여럿이고 고를 근거가 없으면 고르지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import handover_release as release, workstate as ws
from .output_policy import validate_output
from .paths import relative_to_source

PACKAGE_FORMAT = 1
MANIFEST, SNAPSHOT, REPORT = "manifest.json", "snapshot.json", "report.md"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
NOTICE = ("이 묶음에는 문서에서 옮겨 적은 인용문이 들어 있습니다. 공개 파일로 다루지 "
          "마세요. 원문 파일은 들어 있지 않습니다.")

# 재연결 결과
EXACT, MOVED, CHANGED, AMBIGUOUS, MISSING = "exact", "moved", "changed", "ambiguous", "missing"
RELINK_LABEL = {
    EXACT: "같은 자리에서 찾음", MOVED: "옮겨진 자리에서 찾음",
    CHANGED: "같은 이름이지만 내용이 다름", AMBIGUOUS: "같은 내용의 파일이 여럿 — 고르지 않음",
    MISSING: "찾지 못함",
}


class PackageError(ValueError):
    """묶음을 받아들일 수 없다. 메시지는 사용자에게 그대로 보여도 되는 한글이다."""


# ── 내보내기 ────────────────────────────────────────────────────────

def export_package(db, snapshot_id: int, path: str | Path) -> Path:
    """저장한 판 하나를 묶음 파일로 내보낸다. 원본 자료 폴더에는 쓰지 않는다."""
    from ..db.migrations import SCHEMA_VERSION

    row = db.snapshot(snapshot_id)
    if row is None:
        raise PackageError("내보낼 복원본을 찾을 수 없습니다.")
    payload_text = row["payload"]
    report = release.to_markdown(json.loads(payload_text), row["kind"])
    members = {SNAPSHOT: payload_text.encode("utf-8"), REPORT: report.encode("utf-8")}
    manifest = {
        "package_format": PACKAGE_FORMAT, "schema_version": SCHEMA_VERSION,
        "kind": row["kind"], "basis_at": row["basis_at"], "content_hash": row["content_hash"],
        "notice": NOTICE,
        "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                  for name, data in members.items()},
    }
    members[MANIFEST] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    target = validate_output(path, db)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=target.parent, prefix=".export-", delete=False) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in (MANIFEST, SNAPSHOT, REPORT):
                archive.writestr(name, members[name])
        validate_output(target, db)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    db.audit("work.package.export", str(snapshot_id))
    return target


# ── 읽기와 검사 ─────────────────────────────────────────────────────

def read_package(path: str | Path) -> tuple[dict, dict]:
    """묶음을 검사하고 (manifest, 스냅샷 본체)를 돌려준다. 아무것도 쓰지 않는다."""
    from ..db.migrations import SCHEMA_VERSION

    path = Path(path)
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise PackageError("눈치코치 묶음 파일이 아닙니다.")
    with zipfile.ZipFile(path) as archive:
        infos = {info.filename: info for info in archive.infolist()}
        for name in infos:
            # 풀지는 않지만, 폴더를 벗어나는 이름이 든 묶음은 통째로 받지 않는다.
            if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name:
                raise PackageError("묶음 안에 허용되지 않는 경로가 있습니다.")
        if not {MANIFEST, SNAPSHOT} <= set(infos):
            raise PackageError("묶음에 필요한 파일이 없습니다.")
        if infos[MANIFEST].file_size > MAX_MANIFEST_BYTES:
            raise PackageError("묶음의 목록 파일이 지나치게 큽니다.")
        if infos[SNAPSHOT].file_size > MAX_MEMBER_BYTES:
            raise PackageError("묶음이 지나치게 큽니다.")
        try:
            manifest = json.loads(_read(archive, infos[MANIFEST], MAX_MANIFEST_BYTES))
            body = _read(archive, infos[SNAPSHOT], MAX_MEMBER_BYTES)
        except (ValueError, zipfile.BadZipFile, OSError) as exc:
            raise PackageError("묶음을 읽을 수 없습니다.") from exc

    if not isinstance(manifest, dict) or manifest.get("package_format") != PACKAGE_FORMAT:
        raise PackageError("이 앱이 읽을 수 없는 묶음 형식입니다.")
    try:
        newer = int(manifest.get("schema_version", 0)) > int(SCHEMA_VERSION)
    except (TypeError, ValueError) as exc:
        raise PackageError("묶음의 판 정보를 읽을 수 없습니다.") from exc
    if newer:
        raise PackageError("더 새로운 앱에서 만든 묶음입니다. 앱을 최신으로 올린 뒤 가져오세요.")
    expected = (manifest.get("files") or {}).get(SNAPSHOT) or {}
    if (expected.get("sha256") != hashlib.sha256(body).hexdigest()
            or expected.get("size") != len(body)):
        raise PackageError("묶음이 만들어진 뒤에 내용이 바뀌었습니다(해시 불일치).")
    if manifest.get("kind") not in ws.SNAPSHOT_KINDS:
        raise PackageError("알 수 없는 종류의 묶음입니다.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PackageError("묶음의 본체를 읽을 수 없습니다.") from exc
    if (not isinstance(payload, dict) or payload.get("format") != release.FORMAT_VERSION
            or not isinstance(payload.get("tasks"), list)
            or not isinstance(payload.get("documents"), list)):
        raise PackageError("묶음의 본체 형식이 맞지 않습니다.")
    if release.content_hash(payload) != manifest.get("content_hash"):
        raise PackageError("묶음의 지문이 본체와 맞지 않습니다.")
    return manifest, payload


def _read(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """선언된 크기를 믿지 않고, 한도까지만 읽는다(압축 폭탄 방지)."""
    with archive.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise PackageError("묶음이 지나치게 큽니다.")
    return data


# ── 가져오기 ────────────────────────────────────────────────────────

def import_package(db, path: str | Path, receiver_name: str | None = None) -> dict:
    """묶음을 이 프로젝트로 가져온다. 결과 요약을 돌려준다.

    검사를 통과한 뒤에만 쓰고, 쓰는 도중 문제가 생기면 전부 되돌린다.
    """
    manifest, payload = read_package(path)
    if receiver_name is None:
        receiver_name = db.reviewer()[0]
    result = {"tasks": 0, "items": 0, "steps": 0, "gaps": 0, "kept_local": 0}
    try:
        with db.transaction():
            _apply(db, payload, result)
            snapshot_id = db.add_snapshot(
                manifest["kind"], str(payload.get("basis_at") or ""), manifest["content_hash"],
                json.dumps(release.summarize(payload), ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False), label="가져옴",
                context_id=result.pop("context_id"))
            relink = relink_evidence(db)
            db.add_receipt(snapshot_id, manifest["content_hash"], receiver_name,
                           json.dumps(relink, ensure_ascii=False))
    except (KeyError, TypeError, AttributeError, sqlite3.IntegrityError) as exc:
        raise PackageError("묶음의 내용이 형식에 맞지 않아 가져오지 않았습니다.") from exc
    except ValueError as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError(f"묶음의 내용이 형식에 맞지 않아 가져오지 않았습니다: {exc}") from exc
    return {**result, "snapshot_id": snapshot_id, "kind": manifest["kind"], "relink": relink}


def _apply(db, payload: dict, result: dict) -> None:
    con = db.con
    context = payload.get("context") or {}
    mode = ws.require(context.get("mode") or ws.MODE_MATERIALS, ws.MODES, "시작 방식")
    result["context_id"] = db.start_work_context(
        mode, predecessor=context.get("predecessor"), note="가져온 복원본")

    for task in payload["tasks"]:
        name = " ".join(str(task["name"]).split())
        if not name:
            raise ValueError("이름 없는 업무")
        row = con.execute("SELECT id FROM tasks WHERE name = ?", (name,)).fetchone()
        if row is None:
            task_id = con.execute(
                "INSERT INTO tasks(name, description, origin, status, confidence) "
                "VALUES (?, ?, 'seeded', 'proposed', 'medium')",
                (name, str(task.get("description") or ""))).lastrowid
        else:
            task_id = row["id"]
        result["tasks"] += 1

        for item in task["items"]:
            if _place(db, "task_items", "item", task_id, item, result, {
                "kind": str(item["kind"]), "slot": str(item["slot"]),
                "content": str(item["content"]), "auto_content": item.get("auto_content"),
                "extractor": "imported",
            }, key=("kind", "slot")):
                result["items"] += 1

        step_ids: dict[int, int] = {}
        for step in task["steps"]:
            new_id = _place(db, "work_steps", "step", task_id, step, result, {
                "slot": str(step["slot"]), "label": str(step["label"]),
                "auto_label": str(step["label"]) if step.get("origin") != "user" else None,
                "ordinal": int(step.get("ordinal") or 0),
                "inputs": step.get("inputs"), "forms": step.get("forms"),
                "output": step.get("output"), "done_criteria": step.get("done_criteria"),
                "procedure_kind": ws.require(step["procedure_kind"], ws.PROCEDURE, "절차 유형"),
            }, key=("slot",))
            if not new_id:
                continue
            result["steps"] += 1
            step_ids[step["id"]] = new_id
            run = ws.require(step.get("run_state") or ws.NOT_STARTED, ws.RUN, "수행 상태")
            if run != ws.NOT_STARTED:
                # 넘겨준 사람이 실제로 한 기록이다. 시각까지 그대로 옮긴다.
                con.execute(
                    "INSERT OR REPLACE INTO step_runs(step_id, state, started_at, finished_at, "
                    "result_path, note) VALUES (?, ?, ?, ?, ?, ?)",
                    (new_id, run, step.get("started_at"), step.get("finished_at"),
                     step.get("result_path"), step.get("run_note")))

        for gap in task["gaps"]:
            kind = ws.require(gap["kind"], ws.GAP_KINDS, "공백 종류")
            origin = "auto" if gap.get("origin") == "auto" else "user"
            key = str(gap.get("gap_key") or "")
            state = "resolved" if gap.get("state") == "resolved" else "open"
            exists = con.execute(
                "SELECT 1 FROM info_gaps WHERE task_id = ? AND origin = ? AND gap_key = ? "
                "AND field = ? AND detail = ?",
                (task_id, origin, key, str(gap["field"]), str(gap.get("detail") or ""))).fetchone()
            if exists or (origin == "auto" and con.execute(
                    "SELECT 1 FROM info_gaps WHERE task_id = ? AND origin = 'auto' AND gap_key = ?",
                    (task_id, key)).fetchone()):
                continue
            con.execute(
                "INSERT INTO info_gaps(task_id, step_id, kind, origin, gap_key, field, detail, "
                "research, next_action, state, resolution, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "CASE WHEN ? = 'resolved' THEN datetime('now') END)",
                (task_id, step_ids.get(gap.get("step_id")), kind, origin, key, str(gap["field"]),
                 str(gap.get("detail") or ""), gap.get("research"), gap.get("next_action"),
                 state, gap.get("resolution"), state))
            result["gaps"] += 1


def _place(db, table: str, owner: str, task_id: int, source: dict, result: dict,
           columns: dict, key: tuple[str, ...]) -> int | None:
    """항목·단계 하나를 놓는다. 받는 쪽에서 사람이 손댄 것이 있으면 그대로 둔다."""
    con = db.con
    where = " AND ".join(f"{name} = ?" for name in key)
    existing = con.execute(
        f"SELECT * FROM {table} WHERE task_id = ? AND {where}",
        (task_id, *(columns[name] for name in key))).fetchone()
    if existing is not None:
        if db._touched(table, existing):
            result["kept_local"] += 1
            return None
        con.execute(f"DELETE FROM {table} WHERE id = ?", (existing["id"],))

    columns = {
        **columns, "task_id": task_id, "context_id": result["context_id"],
        "origin": "user" if source.get("origin") == "user" else "auto",
        "basis_state": ws.require(source["basis_state"], ws.BASIS, "근거 상태"),
        "applicability": ws.require(source["applicability"], ws.APPLICABILITY, "적용 시점"),
        "human_state": ws.require(source["human_state"], ws.HUMAN, "사람 확인"),
        "version": int(source.get("version") or 1),
    }
    marks = ", ".join("?" * len(columns))
    row_id = con.execute(
        f"INSERT INTO {table}({', '.join(columns)}) VALUES ({marks})",
        list(columns.values())).lastrowid

    for evidence in source.get("evidence") or []:
        # 문서 번호는 옮기지 않는다 — 받는 쪽의 번호가 아니다. 해시와 이름·위치로 다시 잇는다.
        db.add_evidence(owner, row_id, None, evidence.get("doc_hash"),
                        str(evidence.get("locator") or ""), str(evidence.get("quote") or ""),
                        evidence.get("doc_name"), evidence.get("doc_relpath"))
    for review in source.get("reviews") or []:
        con.execute(
            "INSERT INTO reviews(target_kind, target_id, target_version, action, before_val, "
            "after_val, reviewer_name, reviewer_role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
            (owner, row_id, columns["version"],
             ws.require(review["action"], ws.REVIEW_ACTIONS, "검토 행동"),
             review.get("before"), review.get("after"), str(review.get("name") or ""),
             ws.require(review["role"], ws.CONFIRMED_BY, "검토자 역할"), review.get("at")))
    return row_id


# ── 원문 재연결 ─────────────────────────────────────────────────────

def relink_evidence(db) -> dict[str, int]:
    """끊긴 근거를 이 PC의 문서에 다시 잇는다. 몇 번을 불러도 결과가 같다.

    대상은 문서에 이어져 있지 않거나, 이어진 문서가 사라진 근거다.
      ① 해시가 같은 문서 — 자료원 안의 위치가 같으면 그것, 아니면 이름이 같은 것,
         하나뿐이면 그것. 여럿인데 고를 근거가 없으면 잇지 않는다.
      ② 해시가 같은 문서가 없으면, 같은 위치나 같은 이름의 문서가 하나일 때
         잇는다. 해시가 다르므로 그 근거는 '원문이 바뀜'으로 보인다.
      ③ 그것도 없으면 끊긴 채 둔다. 인용문은 남아 있다.
    """
    counts = {EXACT: 0, MOVED: 0, CHANGED: 0, AMBIGUOUS: 0, MISSING: 0}
    documents = [
        {"id": row["id"], "hash": row["hash"], "name": row["filename"],
         "relpath": relative_to_source(row["path"], row["root"] or "")}
        for row in db.con.execute(
            "SELECT d.id, d.hash, d.filename, d.path, s.path AS root FROM documents d "
            "LEFT JOIN sources s ON s.id = d.source_id "
            "WHERE d.missing_since IS NULL AND d.parse_status != 'skipped'")]
    by_hash: dict[str, list[dict]] = {}
    for document in documents:
        if document["hash"]:
            by_hash.setdefault(document["hash"], []).append(document)

    rows = db.con.execute(
        "SELECT e.id, e.doc_hash, e.doc_name, e.doc_relpath FROM evidence e "
        "LEFT JOIN documents d ON d.id = e.doc_id "
        "WHERE e.doc_id IS NULL OR d.missing_since IS NOT NULL").fetchall()
    for row in rows:
        outcome, doc_id = _match(row, by_hash, documents)
        counts[outcome] += 1
        if doc_id is not None:
            db.con.execute("UPDATE evidence SET doc_id = ? WHERE id = ?", (doc_id, row["id"]))
    if rows:
        db.audit("work.relink", detail="; ".join(f"{k}={v}" for k, v in counts.items()))
    return counts


def _match(row, by_hash: dict, documents: list[dict]) -> tuple[str, int | None]:
    same = by_hash.get(row["doc_hash"] or "", [])
    if same:
        placed = [d for d in same if row["doc_relpath"] and d["relpath"] == row["doc_relpath"]]
        if len(placed) == 1:
            return EXACT, placed[0]["id"]
        named = [d for d in same if row["doc_name"] and d["name"] == row["doc_name"]]
        if len(named) == 1:
            return MOVED, named[0]["id"]
        if len(same) == 1:
            return MOVED, same[0]["id"]
        return AMBIGUOUS, None
    for field, value in (("relpath", row["doc_relpath"]), ("name", row["doc_name"])):
        if value:
            found = [d for d in documents if d[field] == value]
            if len(found) == 1:
                return CHANGED, found[0]["id"]
    return MISSING, None


def candidates(db, doc_hash: str) -> list:
    """같은 내용의 파일이 여럿일 때 사람이 고를 후보."""
    return db.con.execute(
        "SELECT id, filename, path FROM documents "
        "WHERE hash = ? AND missing_since IS NULL ORDER BY path", (doc_hash,)).fetchall()


def choose_document(db, doc_hash: str, doc_id: int) -> int:
    """사람이 고른 문서로, 그 해시의 끊긴 근거를 모두 잇는다. 이은 건수를 돌려준다."""
    document = db.document(doc_id)
    if document is None or document["hash"] != doc_hash:
        raise ValueError("고른 문서의 내용이 그 근거와 같지 않습니다.")
    cursor = db.con.execute(
        "UPDATE evidence SET doc_id = ? WHERE doc_hash = ? AND doc_id IS NULL",
        (doc_id, doc_hash))
    db.audit("work.relink.choose", str(doc_id))
    return cursor.rowcount


def describe_relink(counts: dict[str, int]) -> str:
    """'같은 자리에서 찾음 4건 · 찾지 못함 1건'. 0건인 줄은 적지 않는다."""
    parts = [f"{RELINK_LABEL[key]} {counts.get(key, 0)}건"
             for key in (EXACT, MOVED, CHANGED, AMBIGUOUS, MISSING) if counts.get(key)]
    return " · ".join(parts) or "다시 이을 근거가 없습니다"
