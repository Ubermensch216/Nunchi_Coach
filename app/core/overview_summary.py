"""업무 개요의 종합 요약 — '문장에서 찾은 내용'을 한 덩어리 설명으로 묶는다.

문장을 하나씩 늘어놓으면 후임자는 이 일이 무엇인지 알 수 없다(화면에 문장
수십 줄이 같은 무게로 나열됐다). 문장들을 취합해 500자 이내의 개요로 쓰고,
원문 문장은 그 개요의 근거로 접어 둔다.

  · 모델이 있으면 업무 이름·분장 설명·문장들을 주고 요약과 핵심 키워드를 받는다.
    키워드는 자료(문장·업무 이름·분장)에 실제로 나온 말만 남긴다.
  · 같은 문장 묶음·같은 모델에는 지난 요약을 다시 쓴다(분석마다 흔들리지 않는다).
  · 모델이 없거나 실패하면 규칙으로 만든다 — 자주 나온 낱말을 키워드로, 갈래별
    건수와 대표 문장을 요약으로. 화면은 늘 요약을 먼저 보인다.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

LIMIT = 500                 # 요약 + 키워드 줄을 합친 글자 수 상한
PROMPT = "overview_summary"
_MAX_SENTENCES = 40
_MAX_KEYWORDS = 8
_NUM_PREDICT = 700

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "keywords"],
}

_WORD = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z0-9·]+")
# 낱말 끝의 조사·어미. 긴 것부터 떼어 낸다.
_JOSA = sorted(("으로써", "에서는", "으로는", "에게서", "까지는", "부터는", "이라는", "에서",
                "으로", "에게", "까지", "부터", "처럼", "보다", "하는", "하여", "하고", "하며",
                "되는", "되어", "된", "한", "은", "는", "이", "가", "을", "를", "에", "의",
                "와", "과", "로", "도", "만", "및"), key=len, reverse=True)
_STOP = {
    "확인", "필요", "경우", "위하여", "위해", "협조", "주시기", "바랍니다", "있음", "있습니다",
    "대한", "관련", "해당", "다음", "같이", "아래", "또한", "다만", "등을", "부탁드립니다",
    "합니다", "됩니다", "있도록", "있는", "없는", "통해", "통한", "따라", "따른", "대해", "그리고",
    "요청", "안내", "사항", "내용", "방식", "여부", "이상", "이하", "이내", "기타", "모든", "각각",
    "수행", "진행", "실시", "시행", "검토", "작성", "제출", "처리", "준비", "추진", "개선", "보완",
}


def digest(found: list[dict]) -> str:
    # 복원 때의 순서와 저장소에서 읽은 순서가 다르다 — 순서와 무관하게 견준다.
    return hashlib.sha256("\n".join(sorted(i["content"] for i in found)).encode("utf-8")).hexdigest()


def _meta_key(task_id: int) -> str:
    return f"overview_summary:{task_id}"


def _category(item: dict) -> str:
    return (item.get("slot") or "").split(" · ")[0].strip()


def _stem(word: str) -> str:
    for ending in _JOSA:
        if word.endswith(ending) and len(word) - len(ending) >= 2:
            return word[:-len(ending)]
    return word


def keywords(texts: list[str], limit: int = _MAX_KEYWORDS) -> list[str]:
    """여러 문장에 걸쳐 자주 나온 낱말. 한 문장 안의 반복은 한 번으로 센다."""
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    for index, text in enumerate(texts):
        words = {_stem(w) for w in _WORD.findall(text)}
        for word in words:
            if len(word) >= 2 and word not in _STOP and not word.isdigit():
                counts[word] += 1
                first.setdefault(word, index)
    ranked = sorted(counts, key=lambda w: (-counts[w], first[w], w))
    # 한 번만 나온 낱말은 문장이 적을 때만 쓴다 — 그렇지 않으면 우연한 낱말이 섞인다.
    floor = 2 if len(texts) >= 4 else 1
    picked = [w for w in ranked if counts[w] >= floor]
    # '감사'와 '감사관'처럼 하나가 다른 하나를 품으면 앞의 것만 남긴다.
    out: list[str] = []
    for word in picked:
        if not any(word in kept or kept in word for kept in out):
            out.append(word)
        if len(out) >= limit:
            break
    return out


def _fit(summary: str, words: list[str]) -> tuple[str, list[str]]:
    """키워드 줄과 요약을 합쳐 LIMIT 안에 든다. 요약은 문장 경계에서 자른다."""
    words = list(words)
    while words and len(' · '.join(words)) > LIMIT // 4:
        words.pop()
    room = LIMIT - len(' · '.join(words)) - (1 if words else 0)
    summary = " ".join(summary.split())
    if len(summary) > room:
        cut = summary[:room]
        end = max(cut.rfind(". "), cut.rfind("다."), cut.rfind("음."), cut.rfind("함."))
        summary = cut[:end + 2].strip() if end > room // 2 else cut[:room - 1].rstrip() + "…"
    return summary, words


def fallback(task: dict, found: list[dict]) -> dict:
    """모델 없이 만드는 요약 — 갈래별 건수, 핵심 키워드, 대표 문장."""
    texts = [i["content"] for i in found]
    words = keywords(texts)
    groups = Counter(_category(i) for i in found if _category(i))
    parts = [f"자료 문장 {len(found)}건을 모았습니다"]
    if groups:
        parts[0] += "(" + ", ".join(f"{name} {count}건" for name, count in groups.most_common()) + ")"
    if words:
        parts.append(f"주로 {', '.join(words[:5])}에 관한 내용입니다")
    # 키워드를 가장 많이 품은 문장 둘을 대표로 보인다.
    scored = sorted(texts, key=lambda t: (-sum(w in t for w in words), texts.index(t)))
    lead = [re.sub(r"^[\s▪•○◦\-·□■◆※]+", "", t).strip() for t in scored[:2]]
    summary = ". ".join(parts) + ". 대표 문장: " + " / ".join(f"“{t}”" for t in lead if t)
    summary, words = _fit(summary, words)
    return {"summary": summary, "keywords": words, "by": "rule"}


def _grounded(words, sources: str) -> list[str]:
    plain = re.sub(r"\s+", "", sources)
    out = []
    for word in words if isinstance(words, list) else []:
        if isinstance(word, str):
            word = word.strip()
            if 2 <= len(word) <= 20 and re.sub(r"\s+", "", word) in plain and word not in out:
                out.append(word)
    return out[:_MAX_KEYWORDS]


def refresh(db, task: dict, found: list[dict], client=None) -> dict | None:
    """모델로 요약을 만들어 저장한다. 같은 문장·같은 모델이면 지난 요약을 쓴다."""
    if not found:
        db.set_meta(_meta_key(task["id"]), "")
        return None
    key = digest(found)
    cached = _cached(db, task["id"])
    if client is None:
        return cached if cached and cached.get("summary") and cached.get("sentences") == key else None
    from ..ai.prompts_loader import render

    listed = found[:_MAX_SENTENCES]
    prompt, version = render(
        PROMPT, task_name=task.get("name") or "", charter=(task.get("description") or "없음").strip(),
        sentences="\n".join(f"- [{_category(i) or '문장'}] {i['content']}" for i in listed))
    model = f"{client.gen_model}:{version}"
    same = cached if cached and cached.get("sentences") == key else None
    usable = same if same and same.get("summary") else None
    if same and same.get("by") == model:
        return usable                  # 같은 모델의 답(요약을 못 낸 답이어도)을 다시 묻지 않는다
    data, error, _raw = client.generate_json(prompt, SCHEMA, num_predict=_NUM_PREDICT)
    if error:
        return usable                  # 연결 오류는 다음 분석에서 다시 묻는다
    summary = data.get("summary") if isinstance(data, dict) else None
    if not isinstance(summary, str) or len(summary.strip()) < 20:
        if usable is None:
            db.set_meta(_meta_key(task["id"]), json.dumps(
                {"sentences": key, "by": model, "summary": ""}, ensure_ascii=False))
        return usable
    sources = "\n".join([task.get("name") or "", task.get("description") or ""]
                        + [i["content"] for i in listed])
    words = _grounded(data.get("keywords"), sources)
    if len(words) < 4:
        # 경량 모델은 자료에 없는 말로 키워드를 고쳐 쓰곤 한다(실측: 8개 중 1개만 남음).
        words += [w for w in keywords([i["content"] for i in listed])
                  if not any(w in kept or kept in w for kept in words)][:_MAX_KEYWORDS - len(words)]
    summary, words = _fit(summary, words)
    result = {"sentences": key, "by": model, "summary": summary, "keywords": words}
    db.set_meta(_meta_key(task["id"]), json.dumps(result, ensure_ascii=False))
    return result


def _cached(db, task_id: int) -> dict | None:
    raw = db.get_meta(_meta_key(task_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("summary"), str) else None


def read(db, task: dict, found: list[dict]) -> dict | None:
    """화면에 보일 요약. 지금 문장 묶음에 맞는 모델 요약, 없으면 규칙 요약."""
    if not found:
        return None
    cached = _cached(db, task["id"])
    if cached and cached.get("summary") and cached.get("sentences") == digest(found):
        return cached
    return fallback(task, found)
