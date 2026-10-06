"""Ollama 어댑터.

완전 로컬로만 통신한다. 루프백이 아닌 주소는 거부한다 — 업무 자료가 외부로
나가는 경로를 코드 수준에서 막는다(SEC-002).

이 계층은 예외를 밖으로 던지지 않는다. Ollama가 꺼져 있어도 스캔·검색·중복은
그대로 동작해야 하므로, 실패는 값으로 표현한다.

실측(2026-08, gemma4:e2b Q4_K_M 5.1B / bge-m3):
    통합 분석(제목+분류+연도+요약+키워드 한 번에)   9.8초/문서, 분류 정확도 낮음
    분류만 단독                                     2.5초/문서, 정확도 개선
    임베딩 배치 32                                  67ms/건
따라서 작업을 쪼개고, 전수 처리는 임베딩으로, 생성 모델은 대표 문서에만 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import threading
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_GEN_MODEL = "gemma4:e2b"
DEFAULT_EMBED_MODEL = "bge-m3"

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}

CONNECT_TIMEOUT = 3.0
GENERATE_TIMEOUT = 300.0
EMBED_TIMEOUT = 300.0
EMBED_BATCH = 32

# 생성 모델과 임베딩 모델을 번갈아 부르면 메모리에서 서로를 밀어내 매번 다시
# 올라온다. 실측에서 임베딩 직후의 첫 요약이 10초가 아니라 46초 걸렸다.
# 모델을 잠시 붙잡아 두어 연속 작업에서 재적재를 줄인다.
KEEP_ALIVE = "10m"


@dataclass(slots=True)
class ModelProfile:
    name: str
    parameter_size: str = ""
    quantization: str = ""
    context_length: int = 0
    capabilities: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [self.name]
        if self.parameter_size:
            parts.append(self.parameter_size)
        if self.quantization:
            parts.append(self.quantization)
        if self.context_length:
            parts.append(f"컨텍스트 {self.context_length:,}")
        return " · ".join(parts)


OUTCOME_OK = "ok"
OUTCOME_EMPTY = "empty"                # 응답이 비었다 (사고 과정이 한도를 다 쓴 경우 포함)
OUTCOME_TRUNCATED = "truncated"        # 생성 한도에 걸려 JSON이 중간에 끊겼다
OUTCOME_INVALID = "invalid_json"       # 한도와 무관하게 형식이 틀렸다
OUTCOME_ERROR = "error"                # 연결·시간 초과 등 응답 자체를 못 받았다

# 빈 응답을 한도 탓으로 보고 한 번 더 부를 때의 예산. 사고형 모델은 답을 쓰기
# 전에 한도를 다 써 버릴 수 있다 — 같은 한도로 다시 불러 봐야 같은 결과다.
EMPTY_RETRY_SCALE = 4
EMPTY_RETRY_CAP = 2048
# 이보다 작은 호출(예열 등)은 답을 기대하지 않으므로 다시 부르지 않는다.
EMPTY_RETRY_MIN_BUDGET = 32


@dataclass(slots=True)
class Generation:
    """마지막 생성 호출이 어떻게 끝났는가. 본문·질문은 담지 않는다.

    '실패했다'만으로는 원인을 가를 수 없다. 한도가 모자랐는지, 모델이 형식을
    어겼는지, 연결이 끊겼는지에 따라 고칠 곳이 다르다(구축계획서 B06).
    """

    model: str = ""
    outcome: str = ""
    done_reason: str = ""              # Ollama가 알려 준 종료 사유: stop | length | …
    num_predict: int = 0               # 마지막 시도에 준 생성 한도
    eval_count: int = 0                # 실제로 생성한 토큰 수
    prompt_eval_count: int = 0
    elapsed_ms: int = 0
    attempts: int = 0
    think_disabled: bool = False       # 사고 기능을 끄고 불렀는가

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


@dataclass(slots=True)
class Health:
    ok: bool
    message: str
    models: list[str] = field(default_factory=list)
    generation_ready: bool = False
    embedding_ready: bool = False


class OllamaError(Exception):
    """이 모듈 밖으로 나가지 않는다. 호출자에게는 값으로 알린다."""


class OllamaClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        gen_model: str = DEFAULT_GEN_MODEL,
        embed_model: str = DEFAULT_EMBED_MODEL,
    ):
        self.base_url = _require_loopback(base_url)
        self.gen_model = gen_model
        self.embed_model = embed_model
        self._cancelled = threading.Event()
        self._capabilities: dict[str, list[str]] = {}
        self.last_generation = Generation()

    def _supports(self, capability: str) -> bool:
        """설치된 모델이 스스로 밝힌 기능만 믿는다. 모르면 없는 것으로 본다.

        조회에 성공한 결과만 기억한다 — Ollama가 잠깐 꺼져 있었다고 그 모델을
        끝까지 '기능 없음'으로 굳히지 않는다.
        """
        if self.gen_model not in self._capabilities:
            profile = self.profile()
            if profile is None:
                return False
            self._capabilities[self.gen_model] = profile.capabilities
        return capability in self._capabilities[self.gen_model]

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def _request(self, method: str, endpoint: str, *, timeout: float, **kwargs):
        """동기 호출 인터페이스를 유지하면서 워커의 통신을 취소한다."""
        return asyncio.run(self._request_async(method, endpoint, timeout=timeout, **kwargs))

    async def _request_async(self, method: str, endpoint: str, *, timeout: float, **kwargs):
        if self.cancelled:
            raise httpx.RequestError("요청 취소됨")
        # 기관 PC의 환경 프록시를 따라 문서를 외부로 보내지 않는다.
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout) as client:
            request = asyncio.create_task(client.request(method, f"{self.base_url}{endpoint}", **kwargs))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            try:
                while not request.done():
                    if self.cancelled:
                        raise httpx.RequestError("요청 취소됨")
                    if loop.time() >= deadline:
                        raise httpx.TimeoutException("응답 시간 초과")
                    await asyncio.wait({request}, timeout=0.1)
                return await request
            finally:
                if not request.done():
                    request.cancel()
                    try:
                        await request
                    except asyncio.CancelledError:
                        pass

    # ── 상태 ────────────────────────────────────────────────────────
    def health(self) -> Health:
        """연결과 모델 설치 여부를 확인한다. 절대 예외를 던지지 않는다."""
        try:
            response = self._request("GET", "/api/tags", timeout=CONNECT_TIMEOUT)
            response.raise_for_status()
            names = [m["name"] for m in response.json().get("models", [])]
        except httpx.ConnectError:
            return Health(False, "Ollama에 연결할 수 없습니다. 실행 중인지 확인하세요.")
        except httpx.HTTPError as exc:
            return Health(False, f"Ollama 응답 오류: {exc}")
        except (ValueError, KeyError) as exc:
            return Health(False, f"Ollama 응답을 이해할 수 없습니다: {exc}")

        generation = _has_model(names, self.gen_model)
        embedding = _has_model(names, self.embed_model)
        if generation and embedding:
            message = "연결됨"
        else:
            missing = [
                name for name, ok in
                ((self.gen_model, generation), (self.embed_model, embedding)) if not ok
            ]
            message = f"모델이 없습니다: {', '.join(missing)}"
        return Health(
            ok=generation or embedding,
            message=message,
            models=names,
            generation_ready=generation,
            embedding_ready=embedding,
        )

    def profile(self, model: str | None = None) -> ModelProfile | None:
        target = model or self.gen_model
        try:
            response = self._request(
                "POST", "/api/show", json={"model": target}, timeout=30.0
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return None

        details = data.get("details", {})
        info = data.get("model_info", {})
        context = next(
            (v for k, v in info.items() if k.endswith("context_length")), 0
        )
        return ModelProfile(
            name=target,
            parameter_size=details.get("parameter_size", ""),
            quantization=details.get("quantization_level", ""),
            context_length=int(context or 0),
            capabilities=list(data.get("capabilities", [])),
        )

    # ── 생성 ────────────────────────────────────────────────────────
    def generate_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        num_predict: int = 400,
        temperature: float = 0.0,
    ) -> tuple[dict | None, str | None, str]:
        """JSON Schema를 강제해 구조화 응답을 받는다.

        Ollama의 format 파라미터가 문법을 강제하므로 파싱 실패는 드물다.
        그래도 실패하면 (None, 사유, 원문)으로 돌려준다. 원문을 함께 주는
        이유: 토큰 한도에 걸려 중간에 잘린 응답도 완성된 부분은 건질 수
        있다 — 그 복구는 스키마마다 뜻이 다르므로 호출자(예: search/rag.py)
        가 판단한다. 이 계층은 스키마를 모른다.

        어떻게 끝났는지는 `last_generation`에 남긴다. 사고 기능이 있는 모델은
        끄고 부른다 — 구조화 응답에서는 사고 과정이 생성 한도만 소진한다.
        모든 모델에 강제하지 않고 그 기능을 밝힌 모델에만 적용한다.
        """
        import json
        import time

        started = time.monotonic()
        think_off = self._supports("thinking")
        record = self.last_generation = Generation(
            model=self.gen_model, num_predict=num_predict, think_disabled=think_off,
        )

        def finish(outcome: str):
            record.outcome = outcome
            record.elapsed_ms = int((time.monotonic() - started) * 1000)

        budget = num_predict
        while True:
            record.attempts += 1
            record.num_predict = budget
            payload = {
                "model": self.gen_model,
                "prompt": prompt,
                "format": schema,
                "stream": False,
                "keep_alive": KEEP_ALIVE,
                "options": {"temperature": temperature, "num_predict": budget},
            }
            if think_off:
                payload["think"] = False
            try:
                response = self._request(
                    "POST", "/api/generate", json=payload, timeout=GENERATE_TIMEOUT
                )
                response.raise_for_status()
                body = response.json()
            except httpx.TimeoutException:
                finish(OUTCOME_ERROR)
                return None, "모델 응답 시간 초과", ""
            except httpx.ConnectError:
                finish(OUTCOME_ERROR)
                return None, "Ollama 연결 끊김", ""
            except httpx.HTTPError as exc:
                finish(OUTCOME_ERROR)
                return None, f"모델 호출 실패: {exc}", ""
            except ValueError as exc:
                finish(OUTCOME_ERROR)
                return None, f"응답을 읽을 수 없음: {exc}", ""

            raw = body.get("response") or ""
            record.done_reason = str(body.get("done_reason") or "")
            record.eval_count = int(body.get("eval_count") or 0)
            record.prompt_eval_count = int(body.get("prompt_eval_count") or 0)
            hit_limit = record.done_reason == "length"

            if not raw.strip():
                if (hit_limit and record.attempts == 1
                        and num_predict >= EMPTY_RETRY_MIN_BUDGET and not self.cancelled):
                    budget = min(num_predict * EMPTY_RETRY_SCALE, EMPTY_RETRY_CAP)
                    if budget > num_predict:
                        continue
                finish(OUTCOME_EMPTY)
                if hit_limit:
                    return None, "모델이 생성 한도를 다 쓰고도 답을 내지 못했습니다", ""
                return None, "모델이 빈 응답을 냈습니다", ""

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                if hit_limit:
                    finish(OUTCOME_TRUNCATED)
                    return None, "생성 한도에 걸려 응답이 중간에 끊겼습니다", raw
                finish(OUTCOME_INVALID)
                return None, f"JSON 형식 오류: {exc}", raw
            finish(OUTCOME_OK)
            return data, None, raw

    # ── 임베딩 ──────────────────────────────────────────────────────
    def embed(self, texts: list[str]) -> tuple[list[list[float]] | None, str | None]:
        """배치로 임베딩한다. 배치 32에서 건당 67ms로 가장 효율적이었다."""
        if not texts:
            return [], None
        try:
            response = self._request(
                "POST", "/api/embed",
                json={"model": self.embed_model, "input": texts},
                timeout=EMBED_TIMEOUT,
            )
            response.raise_for_status()
            vectors = response.json().get("embeddings")
        except httpx.TimeoutException:
            return None, "임베딩 시간 초과"
        except httpx.ConnectError:
            return None, "Ollama 연결 끊김"
        except httpx.HTTPError as exc:
            return None, f"임베딩 호출 실패: {exc}"
        except ValueError as exc:
            return None, f"응답을 읽을 수 없음: {exc}"

        if not isinstance(vectors, list) or len(vectors) != len(texts):
            return None, "임베딩 개수가 입력과 다릅니다"
        return vectors, None


def _has_model(installed: list[str], wanted: str) -> bool:
    """생략된 태그는 latest로만 해석한다. 다른 크기·양자화는 같은 모델이 아니다."""
    def canonical(name):
        return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"
    return any(canonical(name) == canonical(wanted) for name in installed)


def _require_loopback(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    if host not in LOOPBACK or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError(
            f"로컬 주소만 허용합니다 (요청: {base_url}). "
            "업무 자료가 외부로 나가지 않도록 강제하는 제약입니다."
        )
    return base_url.rstrip("/")
