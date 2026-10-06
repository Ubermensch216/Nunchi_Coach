"""문장으로만 적힌 내용 찾기 — 모델은 고르기만 하고, 값은 원문 그대로 쓴다.

규칙 기반 추출(reconstruction)은 '제출기한: 2025-09-30'처럼 이름표가 붙은
값만 읽는다. "대금은 준공검사를 마친 뒤 30일 이내에 정산한다" 같은 문장은
놓친다(시나리오 평가에서 놓친 4건이 모두 이런 문장이었다).

모델에게 맡기는 것은 **문장이 어느 갈래인가**(기한·금액·적용 조건·진행 상황·
요령, 또는 해당 없음)뿐이다.
  · 모델은 번호를 매긴 문장 목록에서 번호와 갈래만 돌려준다. 문장을 다시
    쓰게 하지 않는다 — 항목의 값은 문서에 적힌 문장 그대로다.
  · 목록에 없는 번호, 정해 둔 갈래 밖의 답은 버린다(출처 검증).
  · 근거 상태는 '추정'이다. 문장은 문서에 있지만, 그것이 이 업무에 중요한
    내용이라는 판단은 모델의 것이다.
  · 기한·제출처 같은 필수 항목의 공백을 닫지 않고, 어떤 단계도 막거나 풀지
    않는다. 따로 '문장에서 찾은 내용'으로만 보인다.
  · 같은 문장 묶음에는 지난 결과를 다시 쓴다. 분석할 때마다 모델의 답이
    흔들려 항목이 생겼다 사라지지 않게 하고, 모델이 꺼져 있어도 남게 한다.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date

from . import reconstruction as rc, workstate as ws

KIND = rc.STATEMENT
PROMPT = "find_statements"
MAX_SENTENCES = 40
_MIN_LENGTH, _MAX_LENGTH = 12, 200
_MAX_DOCS = 200             # 한 업무에서 훑는 최근 시기 문서 수
_BOILERPLATE_DOCS = 3       # 이만큼의 문서에 되풀이되면 틀 문장이다
_NUM_PREDICT = 900      # 문장 40개 × 한 줄씩

# 갈래 → 화면에 보일 이름. 모델은 이 열쇠 가운데서만 고른다.
CATEGORIES = {
    "deadline": "기한·기간",
    "amount": "금액",
    "condition": "적용 조건",
    "progress": "진행 상황",
    "howto": "작성·처리 요령",
}
NONE = "none"      # 어느 갈래도 아닌 문장. 모델이 문장마다 답하게 하려고 둔다.

SCHEMA = {
    "type": "object",
    "properties": {
        "found": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "type": {"type": "string", "enum": [*CATEGORIES, NONE]},
                },
                "required": ["n", "type"],
            },
        },
    },
    "required": ["found"],
}

_SENTENCE_END = re.compile(r"(?<=[다요음함])\.\s+")
_PREDICATE = re.compile(r"(?:다|요|음|함)\.?$")
_DATE_LINE = re.compile(r"^(?:작성일|시행일자|등록일|기안일)\s*[:：]")


def sentences(db, task_id: int, common: frozenset[str] = frozenset()) -> list[dict]:
    """이 업무의 가장 최근 시기 문서에서, 규칙이 읽지 못한 문장만 추린다.

    여러 문서에 되풀이되는 틀 문장은 뺀다('붙임과 같이 제출합니다', '본 문서는
    ○월 … 단계에서 작성되었다'). 숫자만 다른 문장은 같은 틀로 본다. 실제 모델로
    돌렸을 때 이런 문장이 갈래를 받아 항목 자리를 채웠다 — 어느 문서에나 있는
    말은 이 업무에 대해 알려 주는 것이 없다.
    """
    gathered = _gather(db, task_id)
    in_docs: dict[str, set[int]] = {}
    for item in gathered:
        in_docs.setdefault(_skeleton(item["text"]), set()).add(item["doc_id"])
    out: list[dict] = []
    seen: set[str] = set()
    for item in gathered:
        shape = _skeleton(item["text"])
        if shape in seen or shape in common or len(in_docs[shape]) >= _BOILERPLATE_DOCS:
            continue
        seen.add(shape)
        out.append(item)
        if len(out) >= MAX_SENTENCES:
            break
    return out


def common_skeletons(db, task_ids: list[int]) -> frozenset[str]:
    """둘 이상의 업무에 나오는 문장 틀 — 기관의 서식 문구다."""
    tasks_of: dict[str, set[int]] = {}
    for task_id in task_ids:
        for item in _gather(db, task_id):
            tasks_of.setdefault(_skeleton(item["text"]), set()).add(task_id)
    return frozenset(shape for shape, tasks in tasks_of.items() if len(tasks) >= 2)


def _skeleton(sentence: str) -> str:
    return re.sub(r"\d+", "#", rc._normalize(sentence))


def _gather(db, task_id: int) -> list[dict]:
    documents = db.con.execute(
        "SELECT d.id, d.eff_year FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL "
        "  AND d.parse_status IN ('ok', 'partial') "
        "  AND (d.hash IS NULL OR d.id = (SELECT MIN(x.id) FROM task_docs tx "
        "       JOIN documents x ON x.id = tx.doc_id "
        "       WHERE tx.task_id = td.task_id AND x.hash = d.hash AND x.missing_since IS NULL)) "
        "ORDER BY d.eff_date DESC, d.id", (task_id,)).fetchall()
    if not documents:
        return []
    # 지난 회차의 문장은 그때의 사정이다. 가장 최근 시기의 문서만 본다.
    newest = max((row["eff_year"] or 0) for row in documents)
    out: list[dict] = []
    for document in [row for row in documents if (row["eff_year"] or 0) == newest][:_MAX_DOCS]:
        for section in db.con.execute(
            "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal",
            (document["id"],),
        ):
            out += [{"text": sentence, "doc_id": document["id"],
                     "locator": section["locator"], "doc_year": document["eff_year"]}
                    for sentence in _split(section["text"])]
    return out


def _split(text: str) -> list[str]:
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if lines and rc._TABLE_HEAD.match(lines[0]):
        return []                      # 표는 규칙이 머리글로 읽는다
    out = []
    for line in lines:
        if rc._LABEL_RE.search(line) or _DATE_LINE.match(line):
            continue                   # 이름표가 붙은 줄은 규칙의 몫이다
        for part in _SENTENCE_END.split(line):
            part = part.strip()
            # 제목·목차 같은 줄을 빼려고 서술어로 끝나는 문장만 남긴다.
            if _MIN_LENGTH <= len(part) <= _MAX_LENGTH and _PREDICATE.search(part):
                out.append(part)
    return out


def find(db, task_id: int, client=None, basis: date | None = None,
         common: frozenset[str] = frozenset()) -> list[dict]:
    """문장에서 찾은 내용을 업무 항목 후보로 돌려준다. 저장하지 않는다.

    client가 없거나 모델이 답하지 못하면, 같은 문장 묶음에 대해 지난번에 받은
    결과를 쓴다. 그것도 없으면 빈 목록이다 — 규칙 기반 복원은 그대로 돈다.
    """
    basis = basis or date.today()
    listed = sentences(db, task_id, common)
    if not listed:
        return []
    digest = hashlib.sha256(
        "\n".join(item["text"] for item in listed).encode("utf-8")).hexdigest()
    cached = _cached(db, task_id)
    reusable = cached if cached and cached.get("sentences") == digest else None

    picks = None
    extractor = (reusable or {}).get("extractor", "")
    if client is not None:
        from ..ai.prompts_loader import render

        prompt, version = render(PROMPT, sentences="\n".join(
            f"{number}. {item['text']}" for number, item in enumerate(listed, start=1)))
        extractor = f"{client.gen_model}:{version}"
        if reusable and reusable.get("extractor") == extractor:
            picks = reusable["found"]
        else:
            data, error, _raw = client.generate_json(prompt, SCHEMA, num_predict=_NUM_PREDICT)
            if not error:
                picks = _verified(data, len(listed))
                db.set_meta(_meta_key(task_id), json.dumps(
                    {"sentences": digest, "extractor": extractor, "found": picks}))
    if picks is None:
        if reusable is None:
            return []
        picks, extractor = reusable["found"], reusable.get("extractor", "")

    candidates = []
    for number, category in picks:
        item = listed[number - 1]
        mark = hashlib.sha256(rc._normalize(item["text"]).encode("utf-8")).hexdigest()[:6]
        past = bool(item["doc_year"]) and item["doc_year"] < basis.year
        candidates.append({
            "kind": KIND, "slot": f"{CATEGORIES[category]} · {mark}",
            "content": item["text"],
            # 문장은 문서에 그대로 있다. 추정인 것은 '이것이 중요하다'는 판단이다.
            "basis_state": ws.INFERRED,
            "applicability": ws.PAST if past else ws.UNKNOWN,
            "evidence": [(item["doc_id"], item["locator"], item["text"])],
            "extractor": extractor,
        })
    return candidates


def _verified(data, count: int) -> list[list]:
    """모델의 답에서 믿을 수 있는 것만 남긴다 — 있는 번호, 정해 둔 갈래, 한 번씩."""
    found = data.get("found") if isinstance(data, dict) else None
    picks: list[list] = []
    taken: set[int] = set()
    for entry in found if isinstance(found, list) else []:
        if not isinstance(entry, dict):
            continue
        number, category = entry.get("n"), entry.get("type")
        if (isinstance(number, int) and not isinstance(number, bool)
                and 1 <= number <= count and number not in taken and category in CATEGORIES):
            taken.add(number)
            picks.append([number, category])
    return sorted(picks)


def _meta_key(task_id: int) -> str:
    return f"statements:{task_id}"


def _cached(db, task_id: int) -> dict | None:
    raw = db.get_meta(_meta_key(task_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("found"), list):
        return None
    return data
