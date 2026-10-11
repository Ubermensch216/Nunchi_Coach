"""현재 파싱 문자열과 원문 인용을 대조한다. 관계의 의미 검증과는 별개다."""
from __future__ import annotations

import hashlib
import json
import re

from .ontology import canonical_json


def parse_fingerprint(db, doc_id: int) -> str:
    document = db.document(doc_id)
    digest = hashlib.sha256()
    digest.update(canonical_json({k: document[k] for k in ("parser", "parser_version", "parse_status", "extraction_coverage")}).encode())
    for row in db.con.execute("SELECT kind,ordinal,locator,text,meta FROM document_sections WHERE doc_id=? ORDER BY ordinal,id", (doc_id,)):
        digest.update(canonical_json(dict(row)).encode())
    return digest.hexdigest()


def _normalized(text: str):
    chars, offsets = [], []
    previous_end = 0
    for word in re.finditer(r"\S+", text):
        if chars:
            chars.append(" ")
            offsets.append(previous_end)
        chars.extend(word.group())
        offsets.extend(range(word.start(), word.end()))
        previous_end = word.end()
    return "".join(chars), offsets


def match_quote(db, doc_id: int, doc_hash: str | None, locator: str, quote: str):
    document = db.document(doc_id)
    if (document is None or document["missing_since"] is not None or not doc_hash
            or document["hash"] != doc_hash or document["parse_status"] not in ("ok", "partial") or not quote.strip()):
        return None
    normalized_quote = " ".join(quote.split())
    matches = []
    # 위치 없는 인용을 전역에서 우연히 찾았다고 승인하지 않는다.
    for row in db.con.execute("SELECT kind,ordinal,locator,text,meta FROM document_sections WHERE doc_id=? AND locator=? ORDER BY ordinal,id", (doc_id, locator)):
        text, offsets = _normalized(row["text"])
        start = text.find(normalized_quote)
        if start < 0 or text.find(normalized_quote, start + 1) >= 0:
            continue
        begin, end = offsets[start], offsets[start + len(normalized_quote) - 1] + 1
        # 표에서는 머리글을 포함한 실제 행 구간 전체를 유지한다.
        if row["kind"] == "table":
            begin, end = 0, len(row["text"])
        exact = row["text"][begin:end]
        chunk_ids = [r[0] for r in db.con.execute("SELECT id FROM chunks WHERE doc_id=? AND locator=? AND instr(text,?)>0 ORDER BY id LIMIT 2", (doc_id, locator, exact))]
        matches.append({"exact_quote": exact, "locator": row["locator"],
                        "offsets": {"basis": "section_text", "ordinal": row["ordinal"], "start": begin, "end": end,
                                    "normalization": "whitespace_with_original_offsets", "structure": json.loads(row["meta"] or "{}")},
                        "chunk_id": chunk_ids[0] if len(chunk_ids) == 1 else None})
    return matches[0] if len(matches) == 1 else None
