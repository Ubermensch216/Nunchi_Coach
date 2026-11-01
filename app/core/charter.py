"""업무 체계 — 업무분장표·업무기술서 탐지와 추출.

분장표·기술서가 있으면 그것으로 업무 목록(체계)을 먼저 세운다.
과 전체 분장표인 경우 프로젝트에 등록된 전임자 행만 고르고,
개인정보(성명·연락처)는 일체 항목 값으로 노출하지 않고 담당자 판정에만 쓴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db import Database

CHARTER_KEYWORDS = (
    "업무분장", "사무분장", "사무분담", "업무분담", "업무기술서",
    "직무기술서", "담당업무", "업무현황", "인수인계서", "업무인계"
)
CHARTER_EXTENSIONS = (".hwp", ".hwpx", ".xlsx", ".xls", ".docx", ".pdf")

_HEADER_OWNER = re.compile(r"^(?:담당자|성명|이름|담당|직급|직위)$")
_HEADER_TASK = re.compile(r"^(?:업무|담당업무|사무|주요업무|소관업무|세부업무|분장업무)$")
_HEADER_WEIGHT = re.compile(r"^(?:비중|비율|비중\(%\)|비율\(%\)|%|가중치)$")

_NUM_ORDER_RE = re.compile(r"^\s*[0-9]{1,2}[.)]\s*")
_STAR_BULLET_RE = re.compile(r"^\s*[*•]\s*")
_DASH_BULLET_RE = re.compile(r"^\s*[-ㆍ]\s*")
_ANY_BULLET_RE = re.compile(r"^\s*(?:[○□■●◇◆▶▷\-*ㆍ•]|[0-9]{1,2}[.)]|[가-하][.)])\s*")


@dataclass
class ExtractedItem:
    name: str
    details: list[str] = field(default_factory=list)
    owner: str = ""
    weight: float = 1.0
    locator: str = ""


PREDECESSOR_META = "predecessor_name"
# 분장 항목을 마지막으로 뽑을 때 쓴 전임자 이름. 이름이 바뀌면 다시 뽑는다.
EXTRACTED_FOR_META = "charter_predecessor"


def predecessor_name(db: Database) -> str:
    """분장표에서 고를 전임자 이름. 없으면 빈 문자열이다.

    사용자가 적은 이름이 먼저고, 없으면 전임자와 함께 시작한 맥락의 이름을 쓴다.
    """
    name = (db.get_meta(PREDECESSOR_META) or "").strip()
    if name:
        return name
    context = db.work_context()
    return ((context["predecessor"] or "") if context else "").strip()


def set_predecessor(db: Database, name: str) -> None:
    """분장표에서 고를 전임자 이름을 정한다. 성명은 감사 기록에 남기지 않는다."""
    db.set_meta(PREDECESSOR_META, " ".join((name or "").split()))
    db.audit("charter.predecessor", result="set" if name.strip() else "cleared")


def extract_from_document(db: Database, doc_id: int, predecessor: str | None = None,
                          client=None) -> int:
    """문서 본문에서 분장 항목을 뽑아 저장한다. 저장한 최상위 항목 수를 돌려준다.

    표는 규칙으로 읽는다. 표가 아니면 생성 모델(client)이 있을 때 모델로 읽고,
    없거나 모델이 아무것도 내지 못하면 개조식 머리 줄을 규칙으로 읽는다.
    """
    text = "\n".join(row["text"] for row in db.con.execute(
        "SELECT text FROM document_sections WHERE doc_id = ? ORDER BY ordinal", (doc_id,))
        if row["text"])
    predecessor = predecessor_name(db) if predecessor is None else predecessor
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    items = _extract_from_table(lines, predecessor) if lines else []
    if not items and client is not None and lines:
        items = extract_with_model(text, predecessor, client)
    if not items:
        items = extract_items(text, predecessor)
    save_charter_items(db, doc_id, items)
    return len(items)


MODEL_TEXT_LIMIT = 6000
_NAME_WORD = re.compile(r"[가-힣A-Za-z0-9]{2,}")


def _grounded(phrase: str, text: str) -> bool:
    """모델이 낸 이름의 낱말이 문서에 실제로 있는가. 문서에 없는 업무를 지어내지 않게 한다."""
    words = _NAME_WORD.findall(phrase)
    return bool(words) and sum(word in text for word in words) * 2 >= len(words)


def extract_with_model(text: str, predecessor: str, client) -> list[ExtractedItem]:
    """줄글·개조식 분장 문서에서 생성 모델로 업무를 뽑는다. 실패하면 빈 목록이다."""
    from ..ai.prompts_loader import render
    from ..ai.schemas import CHARTER_SCHEMA

    body = text[:MODEL_TEXT_LIMIT]
    instruction = (f"전임자 이름: {predecessor}. 이 사람의 업무만 추출하세요." if predecessor
                   else "전임자 이름이 주어지지 않았습니다. 문서에 적힌 업무를 모두 추출하세요.")
    prompt, _version = render("charter", target_owner_instruction=instruction, text=body)
    data, error, _raw = client.generate_json(prompt, CHARTER_SCHEMA, num_predict=900)
    if error or not isinstance(data, dict):
        return []
    items: list[ExtractedItem] = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        name = " ".join(str(raw.get("name") or "").split())
        if not (2 <= len(name) <= 60) or not _grounded(name, body):
            continue
        owner = " ".join(str(raw.get("owner") or "").split())
        if predecessor and owner and predecessor not in owner:
            continue
        details = [" ".join(str(d).split()) for d in raw.get("details") or [] if isinstance(d, str)]
        weight = raw.get("weight")
        items.append(ExtractedItem(
            name=name,
            details=[d for d in details if 2 <= len(d) <= 80 and _grounded(d, body)],
            owner=owner if owner and owner in body else "",
            weight=float(weight) if isinstance(weight, (int, float)) and 0 < weight <= 100 else 1.0,
            locator="model",
        ))
    return items


def refresh_items(db: Database, client=None) -> int:
    """전임자 이름이 바뀌었거나 모델을 처음 쓸 수 있게 되면 분장 항목을 다시 뽑는다.

    분장표는 파싱하면서 바로 뽑는다. 그 뒤에 전임자 이름을 적거나 모델이 준비되면
    그때의 결과가 낡으므로, 분석 때마다 여기서 맞춘다. 모델로 뽑아 둔 뒤 모델이
    꺼졌다고 규칙 결과로 되돌리지는 않는다. 다시 뽑은 문서 수를 돌려준다.
    """
    predecessor = predecessor_name(db)
    stored = db.get_meta(EXTRACTED_FOR_META) or ""
    used_model = stored.endswith(_MODEL_MARK)
    if stored.removesuffix(_MODEL_MARK) == predecessor and (used_model or client is None):
        return 0
    done = 0
    for candidate in find_candidates(db):
        if candidate["parse_status"] in ("ok", "partial"):
            extract_from_document(db, candidate["id"], predecessor, client)
            done += 1
    db.set_meta(EXTRACTED_FOR_META, predecessor + (_MODEL_MARK if client is not None else ""))
    return done


_MODEL_MARK = "|model"


def is_charter_candidate(filename: str, path: str = "") -> bool:
    """파일명이나 폴더 경로에 업무분장 키워드가 있고 대상 확장자인지 검사한다."""
    ext = Path(filename).suffix.lower()
    if ext not in CHARTER_EXTENSIONS:
        return False
    fn_lower = filename.lower()
    path_lower = path.lower()
    return any(kw in fn_lower or kw in path_lower for kw in CHARTER_KEYWORDS)


def find_candidates(db: Database) -> list[dict]:
    """DB에 등록된 문서 중 업무분장·기술서 후보 문서를 찾는다."""
    rows = db.con.execute(
        "SELECT id, path, filename, ext, eff_year, fs_mtime, parse_status "
        "FROM documents "
        "WHERE missing_since IS NULL"
    ).fetchall()

    candidates = []
    for r in rows:
        if is_charter_candidate(r["filename"], r["path"]):
            candidates.append(dict(r))
    return candidates


def _split_columns(line: str) -> list[str]:
    """표 한 줄을 셀 단위로 나눈다 (\t, |, 다중 공백)."""
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if "|" in line:
        parts = [c.strip() for c in line.split("|")]
        return [p for p in parts if p]
    return [c.strip() for c in re.split(r"\s{2,}", line) if c.strip()]


def extract_items(text: str, predecessor: str = "") -> list[ExtractedItem]:
    """텍스트에서 전임자 담당 업무 항목들을 추출한다.

    표(table)와 개조식 목록을 모두 지원한다.
    과 전체 분장표에서 predecessor가 지정되어 있다면 그 사람의 업무만 추출한다.
    """
    if not text.strip():
        return []

    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    # 1. 표(Table) 형태 탐지
    table_items = _extract_from_table(lines, predecessor)
    if table_items:
        return table_items

    # 2. 개조식 / 줄글 형태 탐지
    return _extract_from_outline(lines, predecessor)


def _extract_from_table(lines: list[str], predecessor: str) -> list[ExtractedItem]:
    """표 머리글을 찾아 행 단위로 업무를 추출한다."""
    owner_idx = -1
    task_idx = -1
    weight_idx = -1
    header_line_idx = -1
    num_header_cols = 0

    for idx, line in enumerate(lines[:15]):
        cols = _split_columns(line)
        if len(cols) < 2:
            continue
        o_idx = t_idx = w_idx = -1
        for c_idx, col in enumerate(cols):
            clean_col = re.sub(r"[\s_]", "", col)
            if _HEADER_OWNER.match(clean_col):
                o_idx = c_idx
            elif _HEADER_TASK.match(clean_col):
                t_idx = c_idx
            elif _HEADER_WEIGHT.match(clean_col):
                w_idx = c_idx

        if t_idx != -1:
            owner_idx, task_idx, weight_idx = o_idx, t_idx, w_idx
            header_line_idx = idx
            num_header_cols = len(cols)
            break

    if header_line_idx == -1 or task_idx == -1:
        return []

    # 셀 내 개행으로 쪼개진 행들을 복원 병합
    rows_merged: list[tuple[int, list[str]]] = []
    for row_num, line in enumerate(lines[header_line_idx + 1:], start=header_line_idx + 2):
        cols = _split_columns(line)
        if not cols:
            continue

        # 직전 행이 불완전(헤더 컬럼 수 미만)하거나 현재 줄이 계속되는 줄인 경우 병합
        if rows_merged and (len(cols) < num_header_cols - 1 or not re.match(r"^\d+$", cols[0])):
            prev_row_num, prev_cols = rows_merged[-1]
            if len(prev_cols) < num_header_cols:
                # 직전 행의 마지막 컬럼에 현재 줄의 첫 번째를 이어붙이고 나머지 추가
                prev_cols[-1] += "\n" + cols[0]
                prev_cols.extend(cols[1:])
                continue
            elif len(cols) == 1:
                if len(prev_cols) > task_idx:
                    prev_cols[task_idx] += "\n" + cols[0]
                continue

        rows_merged.append((row_num, cols))

    items: list[ExtractedItem] = []
    current_owner = ""

    for row_num, cols in rows_merged:
        if not cols or len(cols) <= task_idx:
            continue

        if owner_idx != -1 and len(cols) > owner_idx and cols[owner_idx]:
            current_owner = cols[owner_idx]

        # 전임자 필터링
        if predecessor:
            if not current_owner or predecessor not in current_owner:
                continue

        task_content = cols[task_idx]
        if not task_content:
            continue

        weight_val = 1.0
        if weight_idx != -1 and len(cols) > weight_idx and cols[weight_idx]:
            m = re.search(r"(\d+(?:\.\d+)?)", cols[weight_idx])
            if m:
                weight_val = float(m.group(1))

        sub_items = _parse_task_content(task_content, current_owner, weight_val, f"row:{row_num}")
        items.extend(sub_items)

    return items


def _parse_task_content(content: str, owner: str, weight: float, locator: str) -> list[ExtractedItem]:
    """한 셀 안의 업무 텍스트를 파싱하여 업무명과 세부업무로 구조화한다."""
    lines = [ln.rstrip() for ln in content.split("\n") if ln.strip()]
    if not lines:
        raw_parts = [p.strip() for p in re.split(r"[,;·/]", content) if p.strip()]
        lines = raw_parts if len(raw_parts) > 1 else [content.strip()]

    result: list[ExtractedItem] = []
    current_parent: ExtractedItem | None = None

    for ln in lines:
        stripped = ln.strip()
        is_num = bool(_NUM_ORDER_RE.match(stripped))
        is_star = bool(_STAR_BULLET_RE.match(stripped))
        is_indented_dash = bool(re.match(r"^\s+[-*ㆍ•]", ln))
        is_sub = is_star or is_indented_dash

        cleaned = _ANY_BULLET_RE.sub("", stripped).strip()
        if not cleaned:
            continue

        line_weight = weight
        wm = re.search(r"\(?\s*(\d+(?:\.\d+)?)\s*%\s*\)?", cleaned)
        if wm:
            line_weight = float(wm.group(1))
            cleaned = cleaned[:wm.start()].strip(" ()")

        if is_sub and current_parent is not None:
            current_parent.details.append(cleaned)
        else:
            item = ExtractedItem(
                name=cleaned,
                details=[],
                owner=owner,
                weight=line_weight,
                locator=locator,
            )
            result.append(item)
            current_parent = item

    return result


def _extract_from_outline(lines: list[str], predecessor: str) -> list[ExtractedItem]:
    """개조식 / 줄글 형태의 분장표 또는 업무기술서에서 추출한다."""
    items: list[ExtractedItem] = []
    in_target_section = not bool(predecessor)
    current_owner = ""

    owner_sec_re = re.compile(
        r"^(?:\[|\(|<)?\s*([가-힣]{2,4})\s*(?:주무관|팀장|사무관|담당|서기관)?\s*(?:\]|\)|>)?$|담당자\s*[:：]\s*([가-힣]{2,4})"
    )

    current_parent: ExtractedItem | None = None

    for row_num, line in enumerate(lines, start=1):
        stripped = line.strip()
        m = owner_sec_re.search(stripped)
        if m:
            found_owner = m.group(1) or m.group(2)
            if found_owner:
                current_owner = found_owner
                if predecessor:
                    in_target_section = (predecessor in current_owner)
                current_parent = None
                continue

        if not in_target_section:
            continue

        cleaned = _ANY_BULLET_RE.sub("", stripped).strip()
        if not cleaned or len(cleaned) < 2:
            continue

        line_weight = 1.0
        wm = re.search(r"\(?\s*(\d+(?:\.\d+)?)\s*%\s*\)?", cleaned)
        if wm:
            line_weight = float(wm.group(1))
            cleaned = cleaned[:wm.start()].strip(" ()")

        is_sub = bool(re.match(r"^\s+[-*ㆍ•]", line)) or (bool(_STAR_BULLET_RE.match(stripped)) and current_parent is not None)
        is_num = bool(_NUM_ORDER_RE.match(stripped))

        if is_num or not is_sub or current_parent is None:
            item = ExtractedItem(
                name=cleaned,
                details=[],
                owner=current_owner,
                weight=line_weight,
                locator=f"row:{row_num}",
            )
            items.append(item)
            current_parent = item
        else:
            current_parent.details.append(cleaned)

    return items


def save_charter_items(db: Database, doc_id: int, items: list[ExtractedItem], decided_by: str = "ai") -> list[int]:
    """추출된 업무분장 항목들을 DB charter_items에 저장한다."""
    saved_ids: list[int] = []
    with db.transaction():
        db.con.execute(
            "DELETE FROM charter_items WHERE doc_id = ? AND decided_by = 'ai'",
            (doc_id,),
        )
        for item in items:
            cur = db.con.execute(
                "INSERT INTO charter_items(doc_id, locator, name, parent_id, owner, weight, decided_by) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?)",
                (doc_id, item.locator, item.name, item.owner, item.weight, decided_by),
            )
            parent_id = cur.lastrowid
            saved_ids.append(parent_id)

            for detail in item.details:
                c_cur = db.con.execute(
                    "INSERT INTO charter_items(doc_id, locator, name, parent_id, owner, weight, decided_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, item.locator, detail, parent_id, item.owner, item.weight, decided_by),
                )
                saved_ids.append(c_cur.lastrowid)

    return saved_ids


def get_charter_items(db: Database, doc_id: int | None = None) -> list[dict]:
    """저장된 업무분장 항목 목록을 조회한다."""
    if doc_id is not None:
        rows = db.con.execute(
            "SELECT id, doc_id, locator, name, parent_id, owner, weight, decided_by "
            "FROM charter_items WHERE doc_id = ? ORDER BY id",
            (doc_id,),
        ).fetchall()
    else:
        rows = db.con.execute(
            "SELECT id, doc_id, locator, name, parent_id, owner, weight, decided_by "
            "FROM charter_items ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]
