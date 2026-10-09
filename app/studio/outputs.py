"""산출물 — 엮은 초안을 저장하고, 고치고, 파일로 내보낸다.

내보낸 파일마다 형식·해시·렌더러 판을 남긴다(output_files). 어떤 본문에서
어떤 파일이 나갔는지 나중에 대조할 수 있어야 한다.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..core import notebook
from ..core.output_policy import validate_output
from . import model, render
from .compose import Draft
from .templates import DEFAULT_FORMAT, Template

STATE_LABELS = {"ai_draft": "AI 초안", "edited": "수정함"}


def save_draft(db, notebook_id: int, template: Template, draft: Draft) -> int:
    notebook.require(db, notebook_id)
    payload = {"markdown": draft.markdown, "format": template.format, "note": draft.note,
               "empty_sections": draft.empty_sections}
    with db.transaction():
        key = db.con.execute(
            "INSERT INTO notebook_outputs(notebook_id,template_id,title,doc_type,model,source_snapshot,citations) "
            "VALUES (?,?,?,?,?,?,?)",
            (notebook_id, template.id, draft.title, template.doc_type, notebook.dumps(payload),
             notebook.dumps(draft.snapshot), notebook.dumps(draft.citations))).lastrowid
        notebook.touch(db, notebook_id)
        db.audit("studio.output.create", f"{notebook_id}/{key}", detail=template.doc_type)
    return key


def outputs(db, notebook_id: int):
    return db.con.execute("SELECT * FROM notebook_outputs WHERE notebook_id=? ORDER BY id DESC",
                          (notebook_id,)).fetchall()


def get(db, output_id: int):
    row = db.con.execute("SELECT * FROM notebook_outputs WHERE id=?", (output_id,)).fetchone()
    if row is None:
        raise ValueError("산출물을 선택하세요.")
    return row


def payload(row) -> dict:
    value = json.loads(row["model"])
    value.setdefault("format", DEFAULT_FORMAT)
    return value


def update(db, output_id: int, markdown: str) -> None:
    """사람이 고친 본문을 저장한다. 제목은 본문의 첫 제목 줄을 따른다."""
    row = get(db, output_id)
    notebook.require(db, row["notebook_id"])
    if not markdown.strip():
        raise ValueError("본문이 비어 있습니다.")
    value = payload(row)
    value["markdown"] = markdown
    db.con.execute(
        "UPDATE notebook_outputs SET model=?, title=?, state='edited', revision=revision+1, "
        "updated_at=datetime('now') WHERE id=?",
        (notebook.dumps(value), model.title_of(markdown, row["title"]), output_id))
    db.audit("studio.output.edit", str(output_id))


def apply_format(db, output_id: int, template: Template) -> None:
    """이미 엮은 산출물에 다른 서식의 모양(글꼴·크기·기호)을 입힌다. 본문의 글은 건드리지 않는다."""
    row = get(db, output_id)
    notebook.require(db, row["notebook_id"])
    value = payload(row)
    value["format"] = template.format
    db.con.execute("UPDATE notebook_outputs SET model=?, template_id=?, revision=revision+1, "
                   "updated_at=datetime('now') WHERE id=?", (notebook.dumps(value), template.id, output_id))
    db.audit("studio.output.format", str(output_id), detail=template.title)


def delete(db, output_id: int) -> None:
    row = get(db, output_id)
    notebook.require(db, row["notebook_id"])
    db.con.execute("DELETE FROM notebook_outputs WHERE id=?", (output_id,))
    db.audit("studio.output.delete", str(output_id))


def stale(db, row) -> bool:
    """본문이 실제로 쓰는 근거 가운데 원문이 달라졌거나 연결이 끊긴 것이 있는가."""
    used = model.cited(payload(row)["markdown"])
    proofs = [c for c in json.loads(row["citations"]) if c["index"] in used]
    return bool(proofs) and not notebook.evidence_current(db, row["notebook_id"], proofs, check_file=True)


def export(db, output_id: int, path: str | Path, renderer=None) -> Path:
    """파일로 내보낸다. 형식은 확장자로 정한다. 자료 폴더·원본 위에는 쓰지 않는다."""
    row = get(db, output_id)
    target = validate_output(path, db)
    kind = target.suffix.lower().lstrip(".")
    draw = renderer or render.RENDERERS.get(kind)
    if draw is None:
        raise ValueError(f"지원하지 않는 형식입니다: .{kind}  (가능: {', '.join(sorted(render.RENDERERS))})")
    if stale(db, row):
        raise ValueError("근거로 쓴 원문이 달라졌거나 연결이 끊겼습니다. 본문을 다시 확인한 뒤 내보내세요.")
    value = payload(row)
    data = draw(value["markdown"], value["format"], row["title"])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=target.parent, prefix=".export-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        validate_output(target, db)     # 쓰는 동안 자료 폴더가 바뀌었을 수 있다
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    db.con.execute("INSERT INTO output_files(output_id,revision,format,path,sha256,renderer) VALUES (?,?,?,?,?,?)",
                   (output_id, row["revision"], kind, str(target), hashlib.sha256(data).hexdigest(),
                    render.RENDERER_VERSION))
    db.audit("studio.output.export", str(output_id), detail=kind)
    return target
