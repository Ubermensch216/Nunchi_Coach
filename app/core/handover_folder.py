"""사본 인계 폴더 만들기 — 공문과 PC 자료를 업무·연도별로 모아 내보낸다 (P4b).

원본 읽기 전용 원칙(NFR-SAF-001)
    원본 자료 폴더(`db.sources()`) 안의 파일은 절대 이동·이름 변경·삭제하지 않는다.
    사용자가 선택한 별도 출력 폴더에 사본만 복사한다.

폴더 구조
    <출력 폴더>/
      00_인계_안내.md                전체 업무 목록 · 미분류 파일 위치 · 확인 상태
      manifest.json                  복사한 사본 목록 · 원본 상대 위치 · SHA-256 · 배정 근거
      <업무 이름>/
        00_업무안내.md               이 업무의 수행 초안과 공문·PC자료 목록
        <연도>/
          공문/                      온나라 수집 공문 본문·붙임 사본
          PC자료/                    대응된 PC 파일 사본
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import report, workcard
from .output_policy import UnsafeOutputError, safe_text_writer
from .paths import relative_to_source

README_NAME = "00_인계_안내.md"
TASK_GUIDE_NAME = "00_업무안내.md"
MANIFEST_NAME = "manifest.json"
OFFICIAL_DIR = "공문"
LOCAL_DIR = "PC자료"
UNKNOWN_YEAR = "연도미상"


@dataclass(slots=True)
class CopiedFile:
    doc_id: int
    task_id: int
    task_name: str
    year: str
    category: str                  # official | local
    exported_relpath: str
    source_kind: str
    source_relpath: str
    sha256: str
    size: int
    confidence: str
    evidence: str
    document_number: str = ""
    official_title: str = ""


@dataclass(slots=True)
class HandoverFolderResult:
    root: Path
    tasks_exported: int = 0
    files_copied: int = 0
    official_copied: int = 0
    local_copied: int = 0
    missing_originals: int = 0
    unclassified_count: int = 0
    entries: list[CopiedFile] = field(default_factory=list)


def validate_output_dir(path: str | Path, db) -> Path:
    """출력 폴더가 자료 폴더(`db.sources()`)나 프로젝트 DB와 겹치지 않는지 검증한다."""
    target = Path(path).resolve()
    if target.exists() and not target.is_dir():
        raise UnsafeOutputError("폴더 경로를 선택해 주세요.")
    if os.name == "nt" and any(":" in part for part in target.parts[1:]):
        raise UnsafeOutputError("폴더 경로에 대체 데이터 스트림을 사용할 수 없습니다.")
    for source in db.sources():
        root = Path(source["path"]).resolve()
        if target == root or target.is_relative_to(root) or root.is_relative_to(target):
            raise UnsafeOutputError("원본 자료 폴더 안이나 상위에는 인계 폴더를 만들 수 없습니다. 별도 출력 폴더를 선택해 주세요.")
    db_file = Path(db.path).resolve()
    if target == db_file:
        raise UnsafeOutputError("프로젝트 DB 경로에는 저장할 수 없습니다.")
    for row in db.con.execute("SELECT path FROM documents"):
        orig = Path(row["path"]).resolve()
        if target == orig or orig.is_relative_to(target):
            raise UnsafeOutputError("원본 자료가 들어 있는 폴더에는 인계 폴더를 만들 수 없습니다.")
    return target


def _safe_name(name: str, fallback: str = "항목") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip()).strip(" .")
    return cleaned[:80] or fallback


def _unique_path(directory: Path, filename: str) -> Path:
    safe = _safe_name(filename, "file")
    candidate = directory / safe
    if not candidate.exists():
        return candidate
    stem = Path(safe).stem
    ext = Path(safe).suffix
    index = 2
    while True:
        candidate = directory / f"{stem}_{index}{ext}"
        if not candidate.exists():
            return candidate
        index += 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()



def _charter_parent_map(db) -> dict[str, str]:
    """charter_items에서 (세부업무 name) -> (대업무 name) 매핑 생성."""
    out = {}
    try:
        rows = db.con.execute("""
            SELECT c.name AS child_name, p.name AS parent_name
            FROM charter_items c
            JOIN charter_items p ON p.id = c.parent_id
        """).fetchall()
        for r in rows:
            out[r["child_name"]] = r["parent_name"]
    except Exception as exc:
        from . import diagnostics
        diagnostics.record("handover_folder.charter_parents.error", exc)
    return out

def _official_meta(db) -> dict[int, dict]:
    """doc_id -> 공문 메타데이터(문서번호·제목·기안일·역할)."""
    out: dict[int, dict] = {}
    for row in db.con.execute(
        "SELECT a.doc_id, a.role, a.filename AS asset_name, r.title, r.document_number, r.authored_at "
        "FROM source_assets a JOIN source_records r ON r.id = a.record_id "
        "WHERE a.doc_id IS NOT NULL ORDER BY a.id"
    ):
        out[row["doc_id"]] = {
            "role": row["role"] or "body",
            "asset_name": row["asset_name"] or "",
            "title": row["title"] or "",
            "document_number": row["document_number"] or "",
            "authored_at": row["authored_at"] or "",
        }
    return out


def _year_of(row, off_meta: dict | None) -> str:
    if off_meta and off_meta.get("authored_at") and re.match(r"^\d{4}", off_meta["authored_at"]):
        return off_meta["authored_at"][:4]
    round_key = (row["round_key"] or "").strip()
    match = re.search(r"(19|20)\d{2}", round_key)
    if match:
        return match.group(0)
    if row["eff_year"]:
        return str(row["eff_year"])
    return UNKNOWN_YEAR


def _export_filename(row, off_meta: dict | None) -> str:
    filename = row["filename"] or Path(row["path"]).name
    if not off_meta:
        return filename
    ext = Path(filename).suffix or row["ext"] or ""
    number = off_meta.get("document_number") or ""
    title = off_meta.get("title") or ""
    role = off_meta.get("role") or "body"
    # 온나라 수집본의 본문 파일명이 '본문.pdf'처럼 일반 이름이면 문서번호와 제목을 붙여 구분한다.
    stem = Path(filename).stem
    if stem in ("본문", "body", "document") or re.fullmatch(r"[0-9a-f]{8,}", stem):
        parts = [p for p in (number, title) if p]
        if parts:
            return _safe_name("_".join(parts)) + ext
    if role != "body" and number and number not in filename:
        return _safe_name(f"{number}_{stem}") + ext
    return filename


def export_handover_folder(
    db,
    output_dir: str | Path,
    *,
    project_name: str = "",
    today: date | None = None,
) -> HandoverFolderResult:
    """업무·연도별 공문/PC자료 사본 인계 폴더를 만든다."""
    today = today or date.today()
    target_root = validate_output_dir(output_dir, db)
    target_root.mkdir(parents=True, exist_ok=True)

    project = project_name or db.get_meta("project_name") or Path(db.path).parent.name
    off_map = _official_meta(db)
    tasks = sorted(db.tasks(), key=lambda t: t["id"])
    result = HandoverFolderResult(root=target_root, unclassified_count=db.unclassified_count())

    parent_map = _charter_parent_map(db)
    used_task_dirs: set[str] = set()
    for task in tasks:
        task_id = task["id"]
        base_dir_name = _safe_name(task["name"], f"업무_{task_id}")
        dir_name = base_dir_name
        suffix = 2
        while dir_name.casefold() in used_task_dirs:
            dir_name = f"{base_dir_name}_{suffix}"
            suffix += 1
        used_task_dirs.add(dir_name.casefold())
        parent_name = parent_map.get(task["name"])
        if parent_name:
            task_dir = target_root / _safe_name(parent_name) / dir_name
        else:
            task_dir = target_root / dir_name
        task_dir.mkdir(parents=True, exist_ok=True)
        result.tasks_exported += 1

        rows = db.con.execute(
            "SELECT d.id, d.path, d.filename, d.ext, d.hash, d.eff_year, d.eff_date, "
            "td.confidence, td.evidence, td.round_key, s.kind AS source_kind, s.path AS source_root "
            "FROM task_docs td "
            "JOIN documents d ON d.id = td.doc_id "
            "JOIN sources s ON s.id = d.source_id "
            "WHERE td.task_id = ? AND d.missing_since IS NULL "
            "ORDER BY COALESCE(d.eff_date, ''), d.id",
            (task_id,),
        ).fetchall()

        task_entries: list[CopiedFile] = []
        missing_in_task: list[str] = []

        for row in rows:
            src_path = Path(row["path"])
            off_info = off_map.get(row["id"])
            is_official = row["source_kind"] == "onnara" or off_info is not None
            category = "official" if is_official else "local"
            sub_label = OFFICIAL_DIR if is_official else LOCAL_DIR
            year_label = _year_of(row, off_info)

            if not src_path.is_file():
                result.missing_originals += 1
                missing_in_task.append(row["filename"] or str(src_path))
                continue

            dest_dir = task_dir / _safe_name(year_label, UNKNOWN_YEAR) / sub_label
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = _unique_path(dest_dir, _export_filename(row, off_info))
            # 다시 한 번 대상 파일이 안전한지 확인 후 사본 복사
            shutil.copy2(src_path, dest_path)
            digest = row["hash"] or _sha256(dest_path)
            size = dest_path.stat().st_size

            entry = CopiedFile(
                doc_id=row["id"],
                task_id=task_id,
                task_name=task["name"],
                year=year_label,
                category=category,
                exported_relpath=dest_path.relative_to(target_root).as_posix(),
                source_kind=row["source_kind"],
                source_relpath=relative_to_source(row["path"], row["source_root"]),
                sha256=digest,
                size=size,
                confidence=row["confidence"] or "medium",
                evidence=row["evidence"] or ("온나라 공문" if is_official else "자료 묶기"),
                document_number=off_info["document_number"] if off_info else "",
                official_title=off_info["title"] if off_info else "",
            )
            result.entries.append(entry)
            task_entries.append(entry)
            result.files_copied += 1
            if is_official:
                result.official_copied += 1
            else:
                result.local_copied += 1

        guide_text = _build_task_guide(db, task, task_entries, missing_in_task, today)
        with safe_text_writer(task_dir / TASK_GUIDE_NAME, db) as handle:
            handle.write(guide_text)

    readme_text = _build_root_readme(db, project, result, today)
    with safe_text_writer(target_root / README_NAME, db) as handle:
        handle.write(readme_text)

    manifest_payload = {
        "format": "nunchi.handover-folder.v1",
        "project": project,
        "exported_at": today.isoformat(),
        "read_only_source_guaranteed": True,
        "summary": {
            "tasks_exported": result.tasks_exported,
            "files_copied": result.files_copied,
            "official_copied": result.official_copied,
            "local_copied": result.local_copied,
            "missing_originals": result.missing_originals,
            "unclassified_count": result.unclassified_count,
        },
        "files": [
            {
                "doc_id": item.doc_id,
                "task_id": item.task_id,
                "task_name": item.task_name,
                "year": item.year,
                "category": item.category,
                "exported_relpath": item.exported_relpath,
                "source_kind": item.source_kind,
                "source_relpath": item.source_relpath,
                "sha256": item.sha256,
                "size": item.size,
                "confidence": item.confidence,
                "evidence": item.evidence,
                "document_number": item.document_number,
                "official_title": item.official_title,
            }
            for item in result.entries
        ],
    }
    with safe_text_writer(target_root / MANIFEST_NAME, db) as handle:
        handle.write(json.dumps(manifest_payload, ensure_ascii=False, indent=2))

    db.audit("handover.folder.export", str(target_root))
    return result


def _build_task_guide(
    db,
    task,
    entries: list[CopiedFile],
    missing_originals: list[str],
    today: date,
) -> str:
    draft = workcard.draft_markdown(db, task["id"], today=today)
    lines = [
        draft.rstrip(),
        "",
        "## 이 폴더에 복사된 사본 목록",
        "",
        "> 원본 파일은 원래 자리에 그대로 두고 사본만 복사했습니다. 배정 근거와 확신도를 함께 적습니다.",
        "",
    ]
    if not entries:
        lines.append("- 복사된 파일이 없습니다.")
    else:
        lines.extend([
            "| 연도 | 구분 | 사본 파일 | 배정 근거 (확신도) | 원본 위치(자료원 기준) |",
            "|---|---|---|---|---|",
        ])
        for item in entries:
            kind_label = "공문" if item.category == "official" else "PC자료"
            rel_in_task = "/".join(item.exported_relpath.split("/")[1:])
            meta = f"{item.evidence} ({item.confidence})"
            if item.document_number:
                meta = f"{item.document_number} · {meta}"
            lines.append(
                f"| {item.year} | {kind_label} | `{rel_in_task}` | {meta} | `{item.source_relpath}` |"
            )
    if missing_originals:
        lines.extend([
            "",
            "### 원본을 찾지 못해 복사하지 못한 파일",
            "",
            *[f"- `{name}`" for name in missing_originals],
        ])
    lines.append("")
    return "\n".join(lines)


def _build_root_readme(
    db,
    project: str,
    result: HandoverFolderResult,
    today: date,
) -> str:
    report_md = report.build(db, project=project, today=today)
    unclassified_rows = db.con.execute(
        "SELECT d.filename, d.path, s.path AS source_root "
        "FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE d.missing_since IS NULL "
        "  AND d.parse_status NOT IN ('skipped', 'pending') "
        "  AND d.parent_id IS NULL "
        "  AND d.id NOT IN ("
        "    SELECT td.doc_id FROM task_docs td JOIN tasks t ON t.id = td.task_id "
        "    WHERE t.not_a_task = 0 AND t.merged_into IS NULL"
        "  ) "
        "ORDER BY d.filename LIMIT 50"
    ).fetchall()

    head = [
        f"# {project} — 사본 인계 폴더 안내",
        "",
        f"- 만든 날짜: {today.isoformat()}",
        f"- 정돈한 업무: {result.tasks_exported}개",
        f"- 복사한 사본: 총 {result.files_copied:,}건 (온나라 공문 {result.official_copied:,}건 · PC자료 {result.local_copied:,}건)",
        "- **원본 보존**: 전임자 PC의 원본 파일은 이동·수정·삭제하지 않고 이 폴더에 사본만 복사했습니다.",
        "- 각 업무 폴더 안의 `00_업무안내.md`와 연도별 `공문/`, `PC자료/` 폴더를 함께 확인하세요.",
        "",
    ]
    if unclassified_rows:
        head.extend([
            f"## 어느 업무에도 배정하지 않은 미분류 파일 ({result.unclassified_count:,}건)",
            "",
            "> 확신이 부족한 문서는 억지로 업무 폴더에 넣지 않았습니다. 원본 폴더의 아래 위치에서 확인할 수 있습니다.",
            "",
        ])
        for row in unclassified_rows:
            rel = relative_to_source(row["path"], row["source_root"])
            head.append(f"- `{rel}`")
        if result.unclassified_count > len(unclassified_rows):
            head.append(f"- 외 {result.unclassified_count - len(unclassified_rows):,}건")
        head.append("")

    comparison_json = db.get_meta("charter_comparison")
    if comparison_json:
        try:
            comp = json.loads(comparison_json)
            charter_only = comp.get("charter_only", [])
            data_only = comp.get("data_only", [])
            if charter_only or data_only:
                head.extend([
                    "## 업무분장표 ↔ 자료 대조 현황",
                    "",
                ])
                if charter_only:
                    head.extend([
                        "### ⚠️ 분장표에는 있으나 PC 자료를 찾지 못한 업무",
                        "> 신설 업무이거나 온나라/공용 폴더 등 다른 저장소에 자료가 있을 수 있습니다.",
                        "",
                        *[f"- **{item['name']}**" for item in charter_only],
                        "",
                    ])
                if data_only:
                    head.extend([
                        "### 📁 자료는 발견되었으나 분장표에 명시되지 않은 업무",
                        "> 전임자가 비공식적으로 수행했거나 이전 담당자로부터 이관된 업무일 수 있습니다.",
                        "",
                        *[f"- **{item['name']}** ({item.get('doc_count', 0)}건)" for item in data_only],
                        "",
                    ])
        except Exception as exc:
            from . import diagnostics
            diagnostics.record("handover_folder.charter_comparison.error", exc)

    head.extend(["---", "", report_md])
    return "\n".join(head)
