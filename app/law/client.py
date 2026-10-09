"""law.go.kr 공개 API 호출 — 법령 검색과 조문 조회.

닿는 주소는 아래 두 개뿐이고 코드에 못 박혀 있다. 설정이나 입력으로 바꿀 수
없다 — 인증키와 질문 문장이 다른 곳으로 가는 길을 만들지 않는다.

오류 문구에서 인증키(OC)를 가린다. 주소에 키가 실려 가므로 예외 문구를 그대로
화면·기록에 올리면 키가 새어 나간다.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from .refs import LawRef, normalize_name

SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"
PUBLIC_URL = "https://www.law.go.kr/lsInfoP.do?lsiSeq={mst}"
TIMEOUT = 8.0
# '현행'을 약속하는 기능이라 하루만 붙들어 둔다. 조회 시점은 답에 함께 적는다.
CACHE_HOURS = 24
MAX_ARTICLE_CHARS = 4000

_NAME_KEYS = ("법령명한글", "법령명_한글", "법령명", "법령약칭명")
_ID_KEYS = ("법령ID",)
_MST_KEYS = ("법령일련번호", "MST")
_TYPE_KEYS = ("법령구분명", "법령구분")
_EFFECTIVE_KEYS = ("시행일자", "시행일")
_ARTICLE_NO_KEYS = ("조문번호",)
_ARTICLE_BRANCH_KEYS = ("조문가지번호", "조문가지")
_ARTICLE_TITLE_KEYS = ("조문제목",)
_TEXT_KEYS = {"조문내용", "조문내용문", "항내용", "호내용", "목내용"}
_OC_IN_TEXT = re.compile(r"([?&]OC=)[^&\s\"']+", re.IGNORECASE)


class LawError(Exception):
    """사용자에게 그대로 보여도 되는 한글 문구를 담는다. kind로 갈래를 가른다."""

    def __init__(self, message: str, kind: str = "upstream"):
        super().__init__(message)
        self.kind = kind   # not_configured | network | upstream | not_found


@dataclass(slots=True)
class LawHit:
    law_name: str
    law_id: str = ""
    mst: str = ""
    law_type: str = ""
    effective_date: str = ""


@dataclass(slots=True)
class LawArticle:
    law_name: str
    article: str            # "제9조" · "제9조의2"
    title: str
    text: str
    effective_date: str = ""
    mst: str = ""
    url: str = ""
    fetched_at: str = ""    # 이 조문을 law.go.kr에서 받은 시각

    @property
    def label(self) -> str:
        head = f"「{self.law_name}」 {self.article}"
        return f"{head}({self.title})" if self.title else head


def _objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _first(node: dict, keys) -> str:
    for key in keys:
        value = node.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    return ""


def _deep(payload: Any, keys) -> str:
    for node in _objects(payload):
        found = _first(node, keys)
        if found:
            return found
    return ""


def _clean(value: Any) -> str:
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", str(value or ""), flags=re.DOTALL)
    text = html.unescape(text)
    # 영문 HTML 태그만 지운다. "<개정 2018.3.27>" 같은 개정 표시는 조문의 일부다.
    text = re.sub(r"</?[a-zA-Z][^<>]*>", " ", text)
    return " ".join(text.split())


def _date(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) == 8 else (value or "").strip()


def _comparable(name: str) -> str:
    return re.sub(r"[\s\-·ㆍ「」『』\"'`“”‘’]", "", name or "").lower()


def _collect_text(value: Any, key: str = "") -> list[str]:
    """조·항·호·목의 글을 문서 순서대로 모은다."""
    pieces: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            pieces += _collect_text(child, child_key)
    elif isinstance(value, list):
        for child in value:
            pieces += _collect_text(child, key)
    elif key in _TEXT_KEYS:
        text = _clean(value)
        if text:
            pieces.append(text)
    return pieces


def mask(text: str, oc: str = "") -> str:
    text = _OC_IN_TEXT.sub(r"\1***", str(text))
    return text.replace(oc, "***") if oc else text


class LawClient:
    def __init__(self, oc: str, db=None, transport: httpx.BaseTransport | None = None,
                 timeout: float = TIMEOUT):
        self.oc = (oc or "").strip()
        self.db = db                    # law_cache 표가 있는 프로젝트 DB. 없으면 캐시하지 않는다.
        self._transport = transport     # 시험에서만 준다
        self._timeout = timeout

    # ── 통신 ────────────────────────────────────────────────────────
    def _get(self, url: str, params: dict[str, Any]) -> Any:
        if not self.oc:
            raise LawError("법령 조회 인증키가 없습니다. 설정 → 시스템에서 입력하세요.", "not_configured")
        query = {"OC": self.oc, "type": "JSON",
                 **{k: v for k, v in params.items() if v not in (None, "")}}
        try:
            # 기관 PC가 인터넷에 나가는 길(프록시)은 환경 설정을 따른다. 로컬 모델 호출과
            # 달리 여기는 일부러 밖으로 나가는 통신이다.
            with httpx.Client(transport=self._transport, timeout=self._timeout,
                              follow_redirects=False) as http:
                response = http.get(url, params=query, headers={"Accept": "application/json"})
        except httpx.TimeoutException as exc:
            raise LawError("법령정보 서버가 제때 응답하지 않았습니다. 잠시 뒤 다시 시도하세요.", "network") from exc
        except httpx.HTTPError as exc:
            raise LawError("법령정보 서버(law.go.kr)에 연결하지 못했습니다. "
                           f"인터넷 연결을 확인하세요. ({type(exc).__name__})", "network") from exc
        if response.status_code != 200:
            raise LawError(f"법령정보 서버가 오류를 돌려주었습니다(HTTP {response.status_code}).", "upstream")
        try:
            payload = response.json()
        except ValueError as exc:
            # 인증키가 틀리면 JSON이 아니라 안내 페이지가 오기도 한다.
            raise LawError("법령정보 서버의 응답을 읽을 수 없습니다. 인증키(OC)가 맞는지 확인하세요.",
                           "upstream") from exc
        for node in _objects(payload):
            result = node.get("result") or node.get("RESULT")
            if result is not None and re.search(r"실패|fail|오류|error", str(result), re.IGNORECASE):
                message = node.get("msg") or node.get("MSG") or result
                raise LawError(f"법령정보 서버: {mask(_clean(message), self.oc)}", "upstream")
        return payload

    # ── 캐시 ────────────────────────────────────────────────────────
    @staticmethod
    def _key(tool: str, params: dict) -> str:
        blob = json.dumps([tool, params], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cached(self, tool: str, params: dict):
        if self.db is None:
            return None
        row = self.db.con.execute("SELECT payload FROM law_cache WHERE key=? AND expires_at > ?",
                                  (self._key(tool, params), datetime.now().isoformat())).fetchone()
        return json.loads(row["payload"]) if row else None

    def _remember(self, tool: str, params: dict, value) -> None:
        if self.db is None:
            return
        now = datetime.now()
        self.db.con.execute(
            "INSERT INTO law_cache(key,tool,payload,fetched_at,expires_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at",
            (self._key(tool, params), tool, json.dumps(value, ensure_ascii=False),
             now.isoformat(timespec="seconds"), (now + timedelta(hours=CACHE_HOURS)).isoformat()))

    # ── 조회 ────────────────────────────────────────────────────────
    def search_law(self, query: str, display: int = 5) -> list[LawHit]:
        """법령 이름으로 찾는다."""
        name = normalize_name(query)
        if not name:
            return []
        params = {"query": name, "display": max(1, min(display, 20))}
        cached = self._cached("search_law", params)
        if cached is None:
            payload = self._get(SEARCH_URL, {"target": "law", **params})
            seen, cached = set(), []
            for node in _objects(payload):
                title = _clean(_first(node, _NAME_KEYS))
                law_id, mst = _first(node, _ID_KEYS), _first(node, _MST_KEYS)
                if not title or not (law_id or mst) or (title, mst) in seen:
                    continue
                seen.add((title, mst))
                cached.append(asdict(LawHit(title, law_id, mst, _clean(_first(node, _TYPE_KEYS)),
                                            _date(_first(node, _EFFECTIVE_KEYS)))))
            self._remember("search_law", params, cached)
        return [LawHit(**item) for item in cached]

    def resolve(self, law_name: str) -> LawHit:
        """이름에 가장 가까운 현행 법령 하나."""
        hits = self.search_law(law_name)
        target = _comparable(normalize_name(law_name))
        for hit in hits:
            if _comparable(hit.law_name) == target:
                return hit
        ranked = []
        for hit in hits:
            name = _comparable(hit.law_name)
            if target not in name and name not in target:
                continue
            # "헌법"이 "헌법재판소 규칙"에 끌려가지 않게, 이름이 그 말로 끝나는 것을 먼저 친다.
            tier = 1 if name.endswith(target) else 2 if name.startswith(target) else 3
            ranked.append((tier, len(name), hit))
        if not ranked:
            raise LawError(f"‘{normalize_name(law_name)}’에 해당하는 현행 법령을 찾지 못했습니다.", "not_found")
        return min(ranked, key=lambda item: item[:2])[2]

    def article(self, ref: LawRef) -> LawArticle:
        """조문 하나의 현행 본문."""
        if ref.article is None:
            raise LawError("조문 번호가 필요합니다.", "not_found")
        hit = self.resolve(ref.law_name)
        params = {"MST": hit.mst, "ID": "" if hit.mst else hit.law_id, "JO": ref.jo_code}
        cached = self._cached("article", params)
        if cached is None:
            payload = self._get(SERVICE_URL, {"target": "lawjosub", **params})
            chosen = None
            for node in _objects(payload):
                number = re.sub(r"\D", "", _first(node, _ARTICLE_NO_KEYS))
                if not number:
                    continue
                branch = re.sub(r"\D", "", _first(node, _ARTICLE_BRANCH_KEYS)) or "0"
                if f"{int(number):04d}{int(branch):02d}" == ref.jo_code:
                    chosen = node
                    break
            text = "\n".join(dict.fromkeys(_collect_text(chosen))) if chosen else ""
            if not text:
                raise LawError(f"「{hit.law_name}」에서 {ref.article_label}를 찾지 못했습니다. "
                               "조문 번호가 바뀌었거나 삭제된 조문일 수 있습니다.", "not_found")
            cached = asdict(LawArticle(
                law_name=_clean(_deep(payload, _NAME_KEYS)) or hit.law_name, article=ref.article_label,
                title=_clean(_first(chosen, _ARTICLE_TITLE_KEYS)), text=text[:MAX_ARTICLE_CHARS],
                effective_date=_date(_deep(payload, _EFFECTIVE_KEYS)) or hit.effective_date, mst=hit.mst,
                url=PUBLIC_URL.format(mst=hit.mst) if hit.mst else "",
                fetched_at=datetime.now().isoformat(timespec="seconds")))
            self._remember("article", params, cached)
        return LawArticle(**cached)

    def search_articles(self, question: str, display: int = 5) -> list[LawRef]:
        """질문 문장으로 관련 조문을 찾는다(law.go.kr의 지능형 검색).

        **질문 문장이 그대로 전송된다.** 부르는 쪽이 사용자가 그 질문에 법령
        조회를 고른 경우에만 불러야 한다.
        """
        text = " ".join(question.split())[:200]
        if not text:
            return []
        params = {"query": text, "display": max(1, min(display, 10))}
        cached = self._cached("search_articles", params)
        if cached is None:
            payload = self._get(SEARCH_URL, {"target": "aiSearch", "search": 0, **params})
            cached = []
            for node in _objects(payload):
                name = _clean(_first(node, _NAME_KEYS))
                number = re.sub(r"\D", "", _first(node, _ARTICLE_NO_KEYS))
                if not name or not number or not 0 < int(number) < 10000:
                    continue
                branch = re.sub(r"\D", "", _first(node, _ARTICLE_BRANCH_KEYS)) or "0"
                entry = [name, int(number), int(branch) or None]
                if entry not in cached:
                    cached.append(entry)
            self._remember("search_articles", params, cached)
        return [LawRef(name, number, branch) for name, number, branch in cached]
