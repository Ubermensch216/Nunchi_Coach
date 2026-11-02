"""사용자가 적은 업무 분장의 어느 항목에 속하는지 생성 모델이 판정한다.

규칙(해시·문서번호·이름 포함)이 확정하지 못한 것만 여기로 온다. 선택지는 분장 항목뿐이고,
모델은 번호를 고르거나 '해당 없음(0)'을 고른다. 목록 밖 답, 근거가 자료에 없는 답, 확신이
낮은 답은 버린다 — 틀린 배정보다 '분장 밖 후보'로 남기는 쪽이 낫다.

같은 분장과 같은 자료는 판정을 다시 묻지 않는다(지문 캐시). 모델이 없으면 None이다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .prompts_loader import render
from .schemas import CHARTER_MATCH_SCHEMA

MATERIAL_LIMIT = 2500
ACCEPT = ("high", "medium")


@dataclass(slots=True)
class Verdict:
    item_id: int | None        # None이면 해당 없음
    confidence: str
    evidence: str
    cached: bool = False


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def material_text(label: str, titles: list[str] = (), folders: list[str] = (), facts: list[str] = ()) -> str:
    parts = [f"묶음 이름: {label}"]
    if titles:
        parts.append("문서 제목: " + " / ".join(dict.fromkeys(titles[:12])))
    if folders:
        parts.append("폴더: " + " / ".join(dict.fromkeys(folders[:6])))
    if facts:
        parts.append("공문 본문에서 읽은 내용:\n- " + "\n- ".join(facts[:8]))
    return "\n".join(parts)[:MATERIAL_LIMIT]


def _fingerprint(items: list[dict], material: str) -> str:
    raw = json.dumps([[i["id"], i["name"], i.get("details", [])] for i in items], ensure_ascii=False) + material
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def judge(db, client, items: list[dict], material: str) -> Verdict | None:
    """items: [{id, name, details[]}]. 판정을 못 내면(모델 실패) None, 해당 없음이면 Verdict(None, …)."""
    if client is None or not items or not material.strip():
        return None
    key = "charter_match:" + _fingerprint(items, material)
    stored = db.get_meta(key)
    if stored:
        try:
            data = json.loads(stored)
            return Verdict(data["item_id"], data["confidence"], data["evidence"], cached=True)
        except (ValueError, KeyError):
            pass
    listing = "\n".join(
        f"{n}. {item['name']}" + (f" — 세부업무: {', '.join(item['details'])}" if item.get("details") else "")
        for n, item in enumerate(items, start=1))
    prompt, _version = render("charter_match", items=listing, material=material)
    data, error, _raw = client.generate_json(prompt, CHARTER_MATCH_SCHEMA, num_predict=200)
    if error or not isinstance(data, dict):
        return None
    choice = data.get("choice")
    confidence = data.get("confidence") if data.get("confidence") in ("high", "medium", "low") else "low"
    evidence = " ".join(str(data.get("evidence") or "").split())[:300]
    if not isinstance(choice, int) or not 0 <= choice <= len(items):
        return None                                  # 목록 밖 번호
    item_id = items[choice - 1]["id"] if choice else None
    if item_id is not None and (confidence not in ACCEPT or len(evidence) < 4
                                or _compact(evidence) not in _compact(material)):
        item_id, confidence = None, "low"            # 근거가 자료에 없거나 확신이 낮다
    verdict = Verdict(item_id, confidence, evidence)
    db.set_meta(key, json.dumps({"item_id": item_id, "confidence": confidence, "evidence": evidence},
                                ensure_ascii=False))
    return verdict


def charter_options(db) -> list[dict]:
    """판정 선택지 — 사용자가 입력한(또는 현재 기준인) 최상위 분장 항목과 세부업무."""
    from ..core import charter
    out = []
    for row in charter.current_items(db):
        details = [r["name"] for r in db.con.execute(
            "SELECT name FROM charter_items WHERE parent_id = ? ORDER BY id", (row["id"],))]
        out.append({"id": row["id"], "name": row["name"], "details": details})
    return out
