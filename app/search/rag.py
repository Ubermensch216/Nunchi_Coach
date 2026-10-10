"""RAG 검색과 답변 — 질문 화면의 엔진.

정체성의 마지막 조각이다. What·When·How가 정형 화면으로 답하지 못하는
질문을 여기서 받는다. 대화가 아니라 단발 질의로 설계한다 — 멀티턴 문맥을
유지하지 않는다. 그래야 챗봇으로 흐르지 않는다(doc/00 §6.4).

정책 (구현 강제)
  · 검색된 프로젝트 자료만 근거로 답한다. 일반 지식으로 빈칸을 채우지 않는다.
  · 사실 문장마다 출처를 남긴다. 출처 없는 답은 만들지 않는다.
  · 근거가 부족하면 답하지 않는다 — 대신 찾은 문서 목록을 보여준다.
  · 자료가 충돌하면 한쪽을 고르지 않는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import numpy as np

from ..ai.client import OllamaClient
from ..ai.settings import project_client
from ..ai.prompts_loader import render
from ..ai.schemas import ASK_SCHEMA
from ..db import Database, representative_predicate
from . import evidence, fts, intent, tablemath
from ..core import diagnostics, korean, provenance
from ..core.chunking import MAX_CHUNK_CHARS
from .query import QueryScope, infer_scope
from .rerank import Candidate, collapse_near_duplicates, rerank
from .vector import normalize, unpack
from .verify import VerifiedSentence, verify_sentences
from .trace import RetrievalTrace

TOP_K = 8
# 실측으로 보정한 값(R7). 옛 임시값 0.30은 아무것도 거르지 못했다 — 대부분의
# 질의에서 138개 조각이 전부 이 선을 넘었다. 평가셋 8개의 최고 유사도 분포:
#     답해야 하는 질문   0.490 ~ 0.672
#     유보해야 하는 질문 0.316 ~ 0.474
# 0.40은 두 무리 사이에 놓이되 답해야 하는 쪽(최저 0.490)과 0.09의 여유를
# 둔다. 이 선을 넘기면 '자료에 없는 주제'가 생성 단계까지 가지 않고 검색
# 단계에서 걸러진다 — 더 빠르고, 모델의 판단에 덜 기댄다.
#
# 주의: 합성 표본 138조각·질문 8개로 잡은 값이다. 실제 기관 자료로
# 파일럿할 때 같은 방법으로 다시 재야 한다(app/tools/rag_report.py).
MIN_SIMILARITY = 0.40
# 조각 하나의 최소 문맥 예산. 앞에서부터 자르지 않고 질문과 관련된 대목을
# 이 안에 골라 담는다(search/evidence.py).
MAX_CONTEXT_CHARS = 400
# 문맥 전체의 기준 예산. 근거가 12건 이하면 이 안에서 조각당 몫을 늘린다.
CONTEXT_TOTAL_CHARS = 4800
RELATED_FALLBACK = 5

# 두 순위(의미·정확 일치)를 이 깊이까지 각각 훑은 뒤 RRF로 합친다. 최종
# 컨텍스트(TOP_K)보다 넉넉히 깊게 봐야 한쪽에서만 강한 후보를 놓치지 않는다.
RANK_POOL = 40
# RRF(Reciprocal Rank Fusion) 상수. 특정 자료에 맞춰 조정한 값이 아니라
# 원 논문(Cormack et al. 2009)의 관행값이다 — 순위 1위와 20위의 점수 차이를
# 완만하게 만들어, 한쪽 순위표의 잡음 하나가 결과를 뒤집지 않게 한다.
RRF_K = 60
# 한 문서가 근거 예산(TOP_K)을 독점하지 않게 한다. 실측에서 근거 8건이
# 사실상 같은 문서의 사본(최종·수정·복사본 등)에 몰린 사례가 있었다
# (§1 측정 2). 완전 동일본은 대표본 판정으로 걸러지지만, 내용이 조금
# 다른 버전은 hash가 달라 그 판정을 피한다 — 그래서 별도로 상한을 둔다.
MAX_CHUNKS_PER_DOC = 2
# 문장 배열 스키마는 플랫 문자열보다 JSON 구조 오버헤드(중괄호·키·배열)가
# 크다. 500이던 옛 한도는 실측(R1)에서 답을 문장 중간에 잘랐다 — 짧은
# 질문 하나가 완전한 답변 없이 "JSON 형식 오류"로 통째로 사라졌다.
GEN_TOKEN_BUDGET = 900
# 대표본·연도 필터·다양성 상한이 후보를 걸러낼 때, 이 깊이보다 아래까지
# 내려가 빈 자리를 채우지 않는다. 실측으로 잡은 회귀: "작년 예산…" 질문에
# 연도 필터가 2022~2024년의 더 좋은 예산 문서를 제외시키자, 우연히
# 2025년인 무관한 문서(행정사무감사)가 그 빈자리를 채웠다 — 걸러서 생긴
# 빈 자리를 무한정 더 깊은 후보로 메우면, 원래라면 답에 안 나왔을 약한
# 근거가 다양성이라는 명목으로 승격된다. 이 한계를 넘으면 그냥 근거
# 개수가 TOP_K보다 적게 나가는 편이 낫다.
EXPLORATION_WINDOW = TOP_K * 2
# 고른 조각의 바로 앞뒤를 함께 넣는다. 조각 하나가 스스로를 설명하지 못하는
# 경우가 있다 — 실측: '[집계] 예산요구액 820'이라는 표 조각에는 연도도
# 업무명도 없고, 그 정보는 바로 앞 본문 조각에만 있다. 표만 근거로 받으면
# 모델은 "2023년 예산 요구액이 얼마야?"에 금액을 답하지 못한다(0/3 실패).
NEIGHBOR_SPAN = 1
# 이 비율보다 많은 문서에 나오는 낱말은 주제를 가르지 못한다고 본다.
# '업무'·'자료'처럼 어느 공문에나 있는 말로 관련성을 재면 무엇이든 통과한다.
GENERIC_TERM_RATIO = 0.5
# 근거 미리보기에 담을 글자 수. 사용자가 "이 문장이 정말 저 문서에서 나왔나"를
# 앱 안에서 확인할 만큼이면 된다 — 그 이상은 원본을 여는 편이 낫다.
SNIPPET_CHARS = 400


@dataclass(slots=True)
class Citation:
    index: int
    doc_id: int
    filename: str
    locator: str
    path: str
    # 근거 문장 자체. 원본을 열지 않고도 앱 안에서 확인할 수 있어야 한다
    # (계획서 §14 — Claim → Evidence → Original 3단계).
    snippet: str = ""
    # 온나라 공식 출처 한 줄(문서번호·상태·본문/붙임). 개인 폴더 자료면 빈 값.
    official: str = ""
    # 긴 문단을 나눈 조각은 locator가 같을 수 있으므로 실제 조각도 고정한다.
    chunk_id: int | None = None


@dataclass(slots=True)
class RelatedDoc:
    doc_id: int
    filename: str
    path: str


@dataclass(slots=True)
class Answer:
    question: str
    text: str
    withheld: bool
    citations: list[Citation] = field(default_factory=list)
    related_docs: list[RelatedDoc] = field(default_factory=list)
    model: str = ""
    error: str | None = None
    # 질문에서 규칙으로 뽑아낸 연도. UI에 직접 보여주진 않지만
    # questions.filters에 남겨 나중에 "이 질문을 왜 이렇게 좁혔는지"
    # 확인할 수 있게 한다.
    inferred_years: list[int] = field(default_factory=list)
    # 생성 호출이 어떻게 끝났는가(종료 사유·토큰 수·소요 시간·복구 여부).
    # 본문은 담지 않는다. 생성까지 가지 않은 답변이면 비어 있다.
    generation: dict = field(default_factory=dict)
    guidance: str = ""

    def __post_init__(self):
        if self.withheld and not self.error and self.generation.get("mode") != "search_only":
            # 저장한 대화를 되살릴 때는 그때 남긴 안내를 쓴다(generation에 실려 온다).
            self.guidance = (self.guidance or self.generation.get("guidance")
                             or needed_material(self.question))


# 보류한 까닭(generation["withheld"])을 화면에 적을 말. '자료가 부족하다'는 자료가
# 정말 없을 때만 쓴다 — 자료는 있는데 다른 까닭으로 보류한 것을 그렇게 적으면 틀린 말이다.
WITHHELD_REASONS = {
    "subject_unknown": "질문한 대상이 선택한 자료에 나오지 않습니다.",
    "model_declined": "찾은 대목만으로는 답을 확정하지 못했습니다.",
    "unverified": "만든 답을 원문과 대조해 확인하지 못해 내놓지 않았습니다.",
    "reason_missing": "찾은 값이 질문에 대한 답인지 확인하지 못했습니다.",
    "no_facts": "선택한 자료에서 요청과 관련된 내용을 찾지 못했습니다.",
}
GENERIC_WITHHELD = "확인 가능한 자료가 부족합니다."


def withheld_reason(answer: "Answer") -> str:
    """보류한 답의 첫 줄. 저장된 답의 글은 바꾸지 않고 보여 줄 때 고른다."""
    text = answer.text or GENERIC_WITHHELD
    if text != GENERIC_WITHHELD:
        return text
    return WITHHELD_REASONS.get(str(answer.generation.get("withheld")), text)


def needed_material(question: str) -> str:
    asked = intent.frame(question, _focus(question))
    if asked.intent == "when":
        material = "해당 회차의 기한·시행일이 명시된 공문이나 변경 안내"
    elif asked.intent in ("amount", "count"):
        material = f"{asked.field or '금액·수량'}의 항목명·단위·대상 기간이 표시된 원문이나 표"
    elif asked.intent == "who":
        material = "담당자·담당 부서 또는 계약 상대가 명시된 문서"
    elif asked.intent == "where":
        material = "제출처·장소가 명시된 공문이나 안내"
    elif asked.intent == "how":
        material = "처리 절차·필요 서식이 적힌 안내와 지난 수행 사례"
    else:
        material = "질문한 대상과 내용을 직접 설명하는 원문"
    return f"확인할 자료 — {material}을 추가하거나, 선택한 업무·연도·자료 범위가 맞는지 확인하세요."


def search_only(db: Database, question: str, scope: QueryScope | None = None,
                message: str = "자료 검색 결과") -> Answer:
    scope = infer_scope(question).merged_with(scope)
    scope.doc_ids = scope.search_documents(db)
    if scope.doc_ids == [] or db.con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0:
        return Answer(question, "", True, error="검색할 자료가 아직 준비되지 않았습니다. 자료 선택과 분석 상태를 확인하세요.",
                      generation={"mode": "search_only"})
    # 한국어 조사 때문에 원문과 다른 꼴의 질문도 핵심어로 검색한다.
    terms = " ".join(korean.stem(word) for word in korean.tokens(question))
    ranked = fts.search_chunk_ids(db, terms, limit=RANK_POOL, doc_ids=scope.doc_ids)
    # 짧은 한국어 핵심어는 trigram으로 못 찾으므로 매개변수 LIKE를 사용한다.
    if not ranked:
        words = [w for w in terms.split() if len(w) >= 2]
        if words:
            clause = " OR ".join("c.text LIKE ? ESCAPE '\\'" for _ in words)
            values = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words]
            restriction = ""
            if scope.doc_ids is not None:
                restriction = " AND d.id IN (" + (",".join("?" for _ in scope.doc_ids) or "NULL") + ")"
                values.extend(scope.doc_ids)
            ranked = [r[0] for r in db.con.execute(
                "SELECT c.id FROM chunks c JOIN documents d ON d.id=c.doc_id "
                f"WHERE d.missing_since IS NULL AND ({clause}){restriction} ORDER BY c.id LIMIT ?",
                (*values, RANK_POOL))]
    selected = _select_context(db, [(cid, 1 / (i + 1)) for i, cid in enumerate(ranked)],
                               set(ranked), scope, question)
    rows = _load_chunk_context(db, [cid for cid, _ in selected])
    official = provenance.official_sources(db, [row["doc_id"] for row in rows])
    citations = [Citation(i, row["doc_id"], row["filename"], row["locator"], row["path"],
                          intent.strip_instructions(evidence.window(row["text"], question, SNIPPET_CHARS))[0],
                          provenance.describe(official.get(row["doc_id"])))
                 # locator만으로는 긴 문단의 어느 조각인지 구별할 수 없다.
                 for i, row in enumerate(rows, 1)]
    for citation, row in zip(citations, rows):
        citation.chunk_id = row["chunk_id"]
    return Answer(question, message if citations else message + " 일치하는 자료를 찾지 못했습니다.",
                  True, citations=citations, inferred_years=scope.years,
                  generation={"mode": "search_only"})


def ask(db: Database, question: str, client: OllamaClient | None = None,
        scope: QueryScope | None = None, expander=None) -> Answer:
    """모든 반환 경로에 원문 없는 검색 진단을 붙인다."""
    trace = RetrievalTrace()
    return trace.finish(_ask(db, question, client, scope, expander, trace))


def _ask(db: Database, question: str, client: OllamaClient | None,
         scope: QueryScope | None, expander, trace: RetrievalTrace) -> Answer:
    """질문 하나에 답한다. 이전 질문을 기억하지 않는다.

    scope는 사용자가 화면에서 직접 고른 범위다. 질문 문장에서 읽어 낸
    범위보다 우선한다 — 화면에서 골랐다는 것은 명시적 의사표시다.

    expander는 질문을 받아 바꿔 쓴 검색 질의 목록을 돌려주는 함수다(노트북
    대화만 준다 — search/expand.py). 규칙으로 답하는 경로에서는 부르지 않는다.
    바꿔 쓴 질의는 후보를 넓히는 데만 쓰고, 답과 검증은 원래 질문으로 한다.
    """
    question = question.strip()
    if not question:
        return Answer(question=question, text="", withheld=True, error="빈 질문입니다")
    content_revision = db.get_meta("content_revision")
    scope = infer_scope(question).merged_with(scope)
    with trace.stage("scope"):
        search_docs = scope.search_documents(db)
    trace.record(scope_documents=len(search_docs) if search_docs is not None else None,
                 years=list(scope.years), task_scoped=scope.has_task)

    client = client or project_client(db, OllamaClient)
    health = client.health()
    if not health.embedding_ready:
        return search_only(db, question, scope, "로컬 AI를 사용할 수 없어 자료 검색 결과를 보여 드립니다.")

    # 업무명·속성이 명시된 단일 값은 전체 허용 자료에서 대조한다.
    # 상충 판본이 근접 중복 축약이나 검색 순위 때문에 사라지기 전에 확인한다.
    with trace.stage("direct_fields"):
        labelled = _labelled_answer(db, question, scope)
    if labelled is not None:
        trace.path = "direct_fields"
        if db.get_meta("content_revision") != content_revision:
            return Answer(question, "", True, error="답변을 준비하는 동안 자료가 갱신되었습니다. 다시 질문해 주세요.")
        return labelled

    with trace.stage("expansion"):
        variants = [v for v in (expander(question) if expander else []) if v and v != question]
    trace.record(query_variants=len(variants))
    with trace.stage("embedding"):
        vectors, error = client.embed([question, *variants])
    if error or not vectors:
        return search_only(db, question, scope, "의미 검색을 사용할 수 없어 자료 검색 결과를 보여 드립니다.")
    query_vectors = normalize(np.asarray(vectors, dtype=np.float32))
    query_vector = query_vectors[0]

    with trace.stage("matrix"):
        chunk_ids, matrix = _load_chunk_matrix(db, client.embed_model, search_docs)
    trace.record(indexed_chunks=len(set(chunk_ids.tolist())), indexed_vector_rows=int(chunk_ids.size),
                 hierarchy_version=1)
    if chunk_ids.size == 0:
        return search_only(db, question, scope, "의미 색인 준비 전입니다. 자료 검색 결과를 보여 드립니다.")

    # 하이브리드 검색 — 의미(벡터)와 정확 일치(FTS5)를 각각 넉넉히 훑어
    # RRF로 합친다. 문서번호·고유명사처럼 정확히 일치해야 하는 질의는
    # 벡터만으로는 놓치기 쉽다(doc/질문화면_RAG_개선계획.md §1 측정 7).
    if matrix.shape[1] != query_vector.shape[0]:
        trace.record(index_dimension_mismatch=True)
        return search_only(db, question, scope, "의미 색인과 검색 모델의 차원이 달라 자료 검색 결과를 보여 드립니다. 색인을 다시 준비하세요.")
    with trace.stage("vector"):
        scores = matrix @ query_vector
        vector_ranked = _parent_vector_rank(chunk_ids, scores)
    with trace.stage("lexical"):
        fts_ranked = fts.search_chunk_ids(db, question, limit=RANK_POOL, doc_ids=search_docs)
    trace.record(vector_candidates=len(vector_ranked), lexical_candidates=len(fts_ranked))
    combined = _reciprocal_rank_fusion(vector_ranked, fts_ranked)

    # '근거로 쓸 만큼 강한가'는 두 검색 중 하나라도 확신을 준 경우다.
    # 의미가 가깝거나(MIN_SIMILARITY 이상), 정확히 그 글자가 상위권에 있거나.
    #
    # FTS 쪽은 fts_ranked 전체(RANK_POOL=40)가 아니라 상위 TOP_K만 본다.
    # OR 결합(escape_match_any)은 토큰 하나만 맞아도 걸리므로, "2025년"처럼
    # 흔한 해와 표기가 겹치는 문서는 40위 안 어딘가에 전부 들어온다 —
    # bm25 자체는 이런 문서를 이미 순위 뒤쪽으로 정확히 낮게 매기는데,
    # 위치를 무시하고 "포함되면 무조건 강함"으로 처리하면 그 순위 정보를
    # 버리는 셈이 된다. 실측: "2025년 행정사무감사…" 질문에 수질통계·
    # 월간실적보고 문서까지 강한 근거로 승격돼 다양성 상한이 그 노이즈를
    # 답변에 끌어들였다(doc/질문화면_RAG_개선계획.md §1-D).
    strong_ids = {cid for cid, score in vector_ranked if score >= MIN_SIMILARITY}
    strong_ids |= set(fts_ranked[:TOP_K])
    if variants:
        with trace.stage("variant_retrieval"):
            combined, widened = _with_variants(
                db, chunk_ids, matrix, query_vectors[1:], variants, vector_ranked, fts_ranked, search_docs)
        trace.record(variant_added_candidates=len(widened - strong_ids))
        strong_ids |= widened

    scope = infer_scope(question).merged_with(scope)
    with trace.stage("selection"):
        strong = _select_context(db, combined, strong_ids, scope, question, trace)
    trace.record(fused_candidates=len(combined), selected_chunks=len(strong))
    trace.record(selected_chunk_ids=[cid for cid, _ in strong])

    if not strong:
        # 범위 밖 자료를 '관련 문서'로 노출하지 않는다.
        related = [] if scope.doc_ids is not None or scope.has_task or scope.has_year else _related_documents(db, [cid for cid, _score in combined[:RELATED_FALLBACK]])
        return Answer(
            question=question,
            text="확인 가능한 자료가 부족합니다.",
            withheld=True,
            related_docs=related,
            inferred_years=scope.years,
        )

    with trace.stage("context"):
        selected = _expand_neighbors(db, [cid for cid, _ in strong], question, scope)
        context_rows = _load_chunk_context(db, selected)
    trace.record(context_chunks=len(context_rows))
    trace.record(selected_doc_ids=sorted({row['doc_id'] for row in context_rows}),
                 context_characters=sum(len(row['text']) for row in context_rows))
    if not context_rows:
        return Answer(question=question, text="확인 가능한 자료가 부족합니다.", withheld=True,
                       inferred_years=scope.years)

    official = provenance.official_sources(db, [row["doc_id"] for row in context_rows])
    # 생성·검증·화면 발췌가 모두 이 글을 본다. 따로 자르면 모델이 본 값과
    # 검증이 본 값이 어긋난다.
    source_text = _evidence_text(context_rows, question)
    # 문서는 자료이지 지시가 아니다. 모델에게 하는 지시로 보이는 문장은 생성 문맥과
    # 검증 양쪽에서 뺀다 — 그 문장의 숫자가 '근거에 있는 숫자'로 검증을 지나가면 안 된다.
    ignored = 0
    for index in list(source_text):
        source_text[index], removed = intent.strip_instructions(source_text[index])
        ignored += removed
    # 질문을 대상·속성·기간·의도로 푼 것. 이유를 묻는 질문의 초점은 이유 검사가 본다.
    asked = intent.frame(question, "" if _asks_reason(question) else _focus(question))
    diagnosis = {"frame": asked.as_record()}
    if variants:
        diagnosis["expansions"] = len(variants)
    if ignored:
        diagnosis["instructions_ignored"] = ignored

    # 표의 한 행에서 가장 큰/작은 값을 묻는 질문은 규칙으로 답한다. 경량 모델은
    # 값 견주기를 자주 틀리고, 틀린 값도 표 안의 숫자라 검증을 지나간다.
    computed, rule = tablemath.extreme(question, source_text), "table_extreme"
    if computed is None:
        # 표의 한 칸에 적힌 수량을 묻는 질문도 같다. 모델은 표에 값이 있는데도
        # "표를 통해 확인할 수 있습니다"라고만 했다(30문항 평가).
        about = _support_text(context_rows, source_text)
        computed, rule = tablemath.count(question, source_text, {
            index: f"{row['filename']}\n{about[index]}"
            for index, row in enumerate(context_rows, start=1)}, stems=asked.stems), "table_cell"
    if computed is not None:
        if asked.intent == 'count':
            from . import quantity
            # 규칙으로 읽은 무단위 표 숫자는 모델의 수량 표현 검사에 보내지 않는다.
            # 다만 인원 역할은 그 표의 원문 행·열과 다시 대조한다.
            support = _support_text(context_rows, source_text)
            values = {v[1] for v in intent.values(computed[0]) if v[0] == 'num'}
            verdicts = [quantity.binding_verdict(asked, computed[0], support.get(n, ''),
                        context_rows[n-1]['filename'], computed_values=values) for n in computed[1]
                        if 0 < n <= len(context_rows)]
            failure = '' if '' in verdicts else 'quantity_role_unbound'
            if failure:
                return Answer(question, '질문한 수량·역할을 원문에서 확인할 수 없어 확정을 보류했습니다.', True,
                              inferred_years=scope.years, generation={**diagnosis, 'withheld': failure})
        text, citations = _compose_answer(
            [VerifiedSentence(text=computed[0], sources=computed[1])],
            context_rows, official, source_text)
        return Answer(question=question, text=text, withheld=False, citations=citations,
                      inferred_years=scope.years, generation={**diagnosis, "rule_answer": rule})

    if _subject_unknown(db, question, asked.field if asked.stems else "", scope):
        # 묻는 대상의 이름이 자료 어디에도 없다. 같은 속성을 가진 다른 대상의 값이
        # 답으로 나가면 안 된다(실측: "정수장 소독약품 구매 단가는 얼마야?"에 사무용품
        # 구매 계약의 "토너의 단가는 58,000원이다"가 나갔다). 모델에 묻지 않고 보류한다.
        related = [RelatedDoc(r["doc_id"], r["filename"], r["path"]) for r in context_rows[:3]]
        return Answer(question=question, text="확인 가능한 자료가 부족합니다.", withheld=True,
                      related_docs=_dedupe(related), inferred_years=scope.years,
                      generation={**diagnosis, "withheld": "subject_unknown"})

    prompt, _version = render(
        "ask", context=_format_context(context_rows, official, source_text), question=question)
    with trace.stage("generation"):
        data, gen_error, raw = client.generate_json(prompt, ASK_SCHEMA, num_predict=GEN_TOKEN_BUDGET)
    trace.record(generation_calls=1)
    generation = {**diagnosis, **_generation_record(client)}
    if db.get_meta("content_revision") != content_revision:
        return Answer(question=question, text="", withheld=True,
                      error="답변을 준비하는 동안 자료가 갱신되었습니다. 다시 질문해 주세요.")

    if data is None and raw:
        # 토큰 한도에 걸려 JSON이 중간에 잘렸을 수 있다 — 완성된 문장까지는
        # 건진다. 통째로 버리면 "짧은 질문 하나가 이유 없이 실패"로 보인다.
        salvaged = _salvage_ask_response(raw)
        if salvaged is not None:
            data, gen_error = salvaged, None
            # 끊긴 응답에서 건진 답이라는 사실을 남긴다 — 온전한 답과 구분한다.
            generation["recovered"] = True

    if gen_error or data is None:
        return Answer(question=question, text="", withheld=True, error=gen_error or "응답 생성 실패",
                       inferred_years=scope.years, generation=generation)

    if not data.get("answered", False) and not getattr(client, "cancelled", False):
        # 같은 질문·같은 근거에 모델이 어떤 때는 답하고 어떤 때는 답할 수 없다고
        # 한다(실측: 같은 질문을 잇달아 두 번 물으면 두 번째에 answered=false가
        # 왔다). 근거로 쓸 만한 조각이 있어 여기까지 온 질문이므로 한 번만 다시
        # 묻는다. 다시 받은 답도 아래의 검증을 그대로 거친다.
        with trace.stage("generation"):
            again, again_error, _raw = _ask_again(client, prompt, ASK_SCHEMA)
        trace.record(generation_calls=trace.counts['generation_calls']+1)
        generation = {**generation, **_generation_record(client), "asked_twice": True}
        if db.get_meta("content_revision") != content_revision:
            return Answer(question=question, text="", withheld=True,
                          error="답변을 준비하는 동안 자료가 갱신되었습니다. 다시 질문해 주세요.")
        if (not again_error and isinstance(again, dict) and again.get("answered")
                and again.get("sentences")):
            data = again

    if not data.get("answered", False):
        generation["withheld"] = "model_declined"
        related = [RelatedDoc(r["doc_id"], r["filename"], r["path"]) for r in context_rows[:3]]
        return Answer(
            question=question,
            text="확인 가능한 자료가 부족합니다.",
            withheld=True,
            related_docs=_dedupe(related),
            model=client.gen_model,
            inferred_years=scope.years,
            generation=generation,
        )

    if not (data.get("sentences") or []) and not getattr(client, "cancelled", False):
        # 답할 수 있다고 하고는 문장을 하나도 내지 않았다. 30문항 평가에서 6문항이
        # 이렇게 끝났고, 같은 조건으로 다시 물으면 같은 빈 목록이 온다. 문장을
        # 하나 이상 내도록 문법을 좁혀 한 번만 다시 묻는다. 그렇게 받은 문장도
        # 아래의 근거 검증을 그대로 거치므로, 근거 없는 문장은 화면에 오르지 않는다.
        with trace.stage("generation"):
            forced, forced_error, _raw = _ask_again(client, prompt, _ASK_SCHEMA_NONEMPTY)
        trace.record(generation_calls=trace.counts['generation_calls']+1)
        generation = {**generation, **_generation_record(client), "empty_answer_retry": True}
        if db.get_meta("content_revision") != content_revision:
            return Answer(question=question, text="", withheld=True,
                          error="답변을 준비하는 동안 자료가 갱신되었습니다. 다시 질문해 주세요.")
        if not forced_error and isinstance(forced, dict) and forced.get("answered"):
            data = forced

    if not (data.get("sentences") or []):
        # 다시 물어도 문장이 없다. 같은 질문·같은 근거에 어떤 때는 옳게 답하고
        # 어떤 때는 빈 목록을 낸다 —
        # 같은 질문·같은 근거에 어떤 때는 옳게 답하고 어떤 때는 빈 목록을 냈다.
        # 이것은 자료가 없는 것이 아니라 생성이 실패한 것이다. '자료 부족'으로
        # 보여 주면 사용자는 문서를 찾으러 간다.
        generation["empty_answer"] = True
        related = [RelatedDoc(r["doc_id"], r["filename"], r["path"]) for r in context_rows[:3]]
        return Answer(
            question=question, text="", withheld=True,
            error="모델이 답변 문장을 만들지 못했습니다. 자료가 없다는 뜻은 아닙니다 — "
                  "다시 질문하거나 아래 문서를 직접 확인해 보세요.",
            related_docs=_dedupe(related), model=client.gen_model,
            inferred_years=scope.years, generation=generation,
        )

    # 모델이 문장·근거를 스스로 짝지어 냈어도, 그 근거가 실제로 그 문장을
    # 뒷받침하는지는 모델이 보장하지 않는다. 여기서 규칙으로 한 번 더
    # 확인한다 — 근거 없는 문장은 화면에 올리지 않는다(§1 측정 1).
    with trace.stage("verification"):
        verified = verify_sentences(
            data.get("sentences") or [], source_text, _support_text(context_rows, source_text),
            question=question,
            names={index: row["filename"] for index, row in enumerate(context_rows, start=1)},
        )
    trace.record(verified_sentences=len(verified.kept), dropped_sentences=len(verified.dropped))

    if verified.reattributed:
        generation["sources_corrected"] = verified.reattributed

    if not verified.survived:
        # 왜 버렸는지를 갈래로만 남긴다 — 사유 문장의 쌍점 뒤(문장의 값)는 넣지 않는다.
        generation["withheld"] = "unverified"
        generation["dropped"] = sorted({d.reason.split(":", 1)[0] for d in verified.dropped})
        related = [RelatedDoc(r["doc_id"], r["filename"], r["path"]) for r in context_rows[:3]]
        return Answer(
            question=question,
            text="확인 가능한 자료가 부족합니다.",
            withheld=True,
            related_docs=_dedupe(related),
            model=client.gen_model,
            inferred_years=scope.years,
            generation=generation,
        )

    if _asks_reason(question) and not any(_GIVES_REASON.search(s.text) for s in verified.kept):
        # 이유를 물었는데 이유를 말하는 문장이 없다. 30문항 평가에서 "집행액이
        # 2분기에 적었던 이유가 뭐야?"에 "2분기 집행액은 210입니다"가 나갔다 —
        # 근거에 있는 숫자지만 묻는 것에 대한 답이 아니다.
        generation["reason_missing"] = True
        generation["withheld"] = "reason_missing"
        related = [RelatedDoc(r["doc_id"], r["filename"], r["path"]) for r in context_rows[:3]]
        return Answer(
            question=question, text="확인 가능한 자료가 부족합니다.", withheld=True,
            related_docs=_dedupe(related), model=client.gen_model,
            inferred_years=scope.years, generation=generation)

    with trace.stage("answer_fit"):
        off = _off_the_point(question, verified.kept, context_rows, source_text, asked)
    if off:
        generation[off] = True
        generation["withheld"] = off
        related = [RelatedDoc(r["doc_id"], r["filename"], r["path"]) for r in context_rows[:3]]
        quantity_problem = off.startswith("quantity_")
        return Answer(
            question=question, text=("질문한 수량과 단위를 답변·원문에서 함께 확인할 수 없어 확정을 보류했습니다."
                                    if quantity_problem else "확인 가능한 자료가 부족합니다."), withheld=True,
            related_docs=_dedupe(related), model=client.gen_model,
            inferred_years=scope.years, generation=generation,
            guidance=("수량·단위·대상 기간이 표시된 원문을 확인하세요. 원문에 값이 있다면 다시 질문해 주세요."
                      if quantity_problem else ""))

    kept = verified.kept
    if asked.intent == 'count':
        matching = [sentence for sentence in kept if not _quantity_failure(asked, sentence, context_rows, source_text)]
        if len(matching) != len(kept):
            generation['sentences_off_quantity'] = len(kept)-len(matching)
        kept = matching
    if asked.stems:
        # 옳은 값을 말한 문장이 있어 답은 나가지만, 다른 속성의 값을 말한 문장은 뺀다.
        verdicts = _wrong_field(asked, kept, context_rows, source_text)
        wrong = [verdict not in ("", _CONFIRMED) for verdict in verdicts]
        if any(wrong):
            generation["sentences_off_field"] = sum(wrong)
            kept = [sentence for sentence, off_field in zip(kept, wrong) if not off_field]
    text, citations = _compose_answer(kept, context_rows, official, source_text)
    return Answer(question=question, text=text, withheld=False, citations=citations,
                  model=client.gen_model, inferred_years=scope.years, generation=generation)


# ── 내부 ────────────────────────────────────────────────────────────

def _labelled_answer(db, question, scope):
    from . import facts

    asked = intent.frame(question, "" if _asks_reason(question) else _focus(question))
    result = facts.resolve(db, asked, scope)
    if result is None:
        return None
    chosen = []
    seen = set()
    for fact in result.facts:
        key = (fact.subject, fact.period, facts.canonical(fact.value))
        if key not in seen:
            chosen.append(fact)
            seen.add(key)
    # 각기 다른 값의 원문을 보여 주되 화면과 생성 문맥을 무한히 늘리지 않는다.
    visible = chosen[:TOP_K]
    official = provenance.official_sources(db, [f.doc_id for f in visible])
    citations = [Citation(index, f.doc_id, f.filename, f.locator, f.path,
                          snippet=f.quote, official=provenance.describe(official.get(f.doc_id)),
                          chunk_id=f.chunk_id) for index, f in enumerate(visible,1)]
    diagnosis = {"frame":asked.as_record(), "documents_checked":result.documents}
    if result.status == "direct_field":
        fact = visible[0]
        period = f"{fact.period}년 자료에 " if fact.period else "자료에 "
        return Answer(question, f"{period}적힌 {fact.label}: {fact.value} [1]", False,
                      citations=citations, inferred_years=scope.years,
                      generation={**diagnosis, "rule_answer":"direct_field"})
    explanations = {
        "evidence_conflict":"같은 업무·기간의 자료에 서로 다른 값이 적혀 있어 하나로 확정할 수 없습니다.",
        "period_ambiguous":"자료의 대상 기간이 달라 적용할 값을 확정할 수 없습니다.",
        "subject_ambiguous":"질문에 여러 업무가 함께 있어 적용할 값을 확정할 수 없습니다.",
        "unresolved_field":"자료에서 해당 값이 아직 결정되지 않은 것으로 표시되어 있습니다.",
        "authority_unresolved":"문서의 이름과 작성 시각만으로 현재 효력이나 최종 승인 상태를 확정할 수 없습니다.",
        "scope_too_broad":"값을 대조할 자료 범위가 넓습니다. 업무·연도·선택 자료를 좁혀 다시 질문하세요.",
        "intent_mismatch":"질문에서 요구한 단위와 원문에 적힌 값의 단위가 달라 답을 확정할 수 없습니다.",
    }
    compared = "\n".join(f"- {f.filename} · {f.period or '기간 미상'} · {f.label}: {f.value} [{i}]"
                         for i,f in enumerate(visible,1))
    if len(chosen)>len(visible):
        compared += "\n일부 근거만 표시했습니다. 선택 자료를 좁혀 다시 확인하세요."
    return Answer(question, explanations[result.status] + ("\n"+compared if compared else ""), True,
                  citations=citations, inferred_years=scope.years,
                  guidance=("질문의 단위와 자료 범위를 확인하세요." if result.status in ('intent_mismatch','scope_too_broad')
                            else "적용 회차와 공식 승인·변경 안내를 확인하거나, 사용할 판본을 직접 선택하세요."),
                  generation={**diagnosis, "withheld":result.status})

# 이유를 묻는 질문과, 이유를 말하는 문장의 표지.
_ASKS_REASON = re.compile(r"왜|이유|원인|까닭|어째서")
_GIVES_REASON = re.compile(r"때문|이유|원인|사유|까닭|인해|탓|덕분|위해|위하여|따라|므로|어서|아서|여서")


def _asks_reason(question: str) -> bool:
    return bool(_ASKS_REASON.search(question))


# 질문이 묻는 것(초점) — 물음말 바로 앞의 낱말, 또는 '어떤/무슨' 바로 뒤의 낱말.
#   "2025년 용역 정산액은 얼마야?" → 정산액    "어떤 불이익이 있어?" → 불이익
_ASK_WORD = r"(?:얼마|뭐|무엇|뭘|언제|어디|누구|누가|어떻게|어땠|몇)"
_FOCUS_BEFORE = re.compile(r"([가-힣A-Za-z]{2,})\s+" + _ASK_WORD)
_FOCUS_AFTER = re.compile(r"(?:어떤|무슨|어느)\s+([가-힣A-Za-z]{2,})")
_PARTICLE = re.compile(r"(?:에서는|에서|으로|이나|은|는|이|가|을|를|의|에|도|로|과|와)$")
# 무엇을 묻는지 가리지 못하는 말. 초점으로 삼지 않는다.
_NO_FOCUS = {"내용", "자료", "문서", "업무", "단계", "순서", "절차", "차례", "방법", "결과", "기준", "현재",
             "그때", "때", "것", "어느", "어떤", "무슨", "매년", "보통", "대략", "모두", "각각"}
_TOPIC_CONNECTORS = {"관련해서", "대해서", "관해서"}


def _focus(question: str) -> str:
    """질문이 값을 묻는 낱말. 정할 수 없으면 빈 문자열(그러면 따지지 않는다)."""
    match = _FOCUS_AFTER.search(question) or _FOCUS_BEFORE.search(question)
    if not match:
        return ""
    word = match.group(1)
    stripped = _PARTICLE.sub("", word)
    if len(stripped) < 2:
        return ""                            # '점이'처럼 한 글자가 남는 말은 초점이 못 된다
    if match.group(1).endswith("적으로"):
        return ""                            # '최종적으로 얼마로'의 '최종적'은 묻는 것이 아니다
    word = stripped
    if word in _TOPIC_CONNECTORS:
        return ""  # '수질 관련해서 뭘'의 연결 표현은 원문 속성명이 아니다.
    if match.group(1)[-1] in "은는도":        # '질의에는' → '질의에' → '질의'
        again = re.sub(r"(?:에서|으로|에|로)$", "", word)
        word = again if len(again) >= 2 else word
    # '내용이야'처럼 흔한 말로 시작하면 무엇을 묻는지 가리지 못한다.
    return "" if any(word.startswith(common) for common in _NO_FOCUS) else word


# 문서가 초점 낱말을 말하는가 — 글자 그대로, 같은 뜻의 말로, 또는 줄인 말로.
# 같은 뜻의 말 묶음과 판정은 search/intent.py에 있다(속성-값 묶임 검사와 함께 쓴다).
_said_in = intent.said_in


# '언제 했어?'에 대한 답이라면 때가 적혀 있어야 한다. '언제까지/언제부터'는 기간·조건으로도
# 답할 수 있어 따지지 않는다("준공검사를 마친 뒤 30일 이내").
_ASKS_WHEN = re.compile(r"언제(?!까지|부터)")
_GIVES_WHEN = re.compile(r"\d+\s*월|(?:19|20)\d{2}|\d+\.\s*\d+|분기|매년|매월|매주|상반기|하반기|연초|연말")


# 한 문장 안의 마디. 날짜의 '2025. 11. 28.'은 가르지 않는다(마침표 뒤가 한글일 때만).
_CLAUSE = re.compile(r"[,;]|(?<=[가-힣])며\s|\.\s+(?=[가-힣])")


def _when_of_something_else(question: str, focus: str, kept: list) -> bool:
    """묻는 것을 말하기는 했는데, 적힌 때는 다른 것의 때인가.

    60문항 평가: "준공검사는 언제 했어?"에 "납기는 2025년 11월 28일이며, 대금은
    준공검사를 마친 뒤 30일 이내에 정산한다" — 때도 있고 '준공검사'도 있지만 같은
    마디에 있지 않다. 문장이 묻는 것을 아예 부르지 않으면("11월 28일입니다") 따지지
    않는다. 질문이 스스로 적은 해('2025년')는 답의 때로 치지 않는다.
    """
    asked = set(re.findall(r"\d+", question))
    named = False
    for sentence in kept:
        for clause in _CLAUSE.split(sentence.text):
            if focus not in re.sub(r"\s+", "", clause):
                continue
            named = True
            rest = re.sub(r"\d+", lambda m: "" if m.group() in asked else m.group(), clause)
            if _GIVES_WHEN.search(rest):
                return False
    return named


def _off_the_point(question: str, kept: list, context_rows: list,
                   source_text: dict[int, str], asked: "intent.QueryFrame | None" = None) -> str:
    """살아남은 문장이 질문이 묻는 것에 대한 답이 아니면 그 까닭. 답이면 빈 문자열.

    문장 하나하나는 근거에 있어 검증을 지나가지만 묻는 것과 다른 것을 말하는
    경우다(60문항 평가: 올해 정산액을 물었는데 계약금액과 지급액을, 늦게 낼 때의
    불이익을 물었는데 제출기한을, 준공검사를 언제 했는지 물었는데 정산 규정을 답했다).
    규칙으로 알 수 있는 것만 본다.
      · 질문이 값을 묻는 낱말(초점)이 인용한 문서 어디에도 없다.
      · 때를 물었는데 어느 문장에도 때가 없다.
      · 때를 물었는데 적힌 때가 묻는 것과 다른 마디에 있다.
      · 답의 값이 문서에서 묶인 이름표·표 칸이 묻는 속성의 것이 아니다(search/intent.py).
    """
    if _ASKS_WHEN.search(question) and not any(_GIVES_WHEN.search(s.text) for s in kept):
        return "when_missing"
    # 이유를 묻는 질문은 위의 이유 검사가 본다.
    focus = "" if _asks_reason(question) else _focus(question)
    if focus and _ASKS_WHEN.search(question) and _when_of_something_else(question, focus, kept):
        return "when_elsewhere"
    if focus:
        cited = {source for sentence in kept for source in sentence.sources}
        docs = {context_rows[source - 1]["doc_id"] for source in cited
                if 0 < source <= len(context_rows)}
        about = "".join(
            row["filename"] + source_text.get(index, "")
            for index, row in enumerate(context_rows, start=1) if row["doc_id"] in docs)
        if not _said_in(focus, about):
            return "focus_missing"
    if asked is not None and asked.stems:
        verdicts = _wrong_field(asked, kept, context_rows, source_text)
        wrong = next((verdict for verdict in verdicts if verdict and verdict != _CONFIRMED), "")
        # 다른 속성의 값을 말한 문장이 있으면, 묻는 속성의 값임이 문서로 확인되는 문장이
        # 따로 있어야 답이다. 곁들인 규정 문장("30일 이내에 정산한다")만 남아서는
        # 묻는 값에 답한 것이 아니다.
        if wrong and _CONFIRMED not in verdicts:
            return wrong
    if asked is not None and asked.intent == "count":
        failures = [_quantity_failure(asked, sentence, context_rows, source_text) for sentence in kept]
        if '' in failures:
            return ''
        if 'quantity_role_unbound' in failures:
            return 'quantity_role_unbound'
        return next((f for f in failures if f != "quantity_missing"), "quantity_missing")
    return ""


def _quantity_failure(asked, sentence, context_rows, source_text):
    from . import quantity
    support = _support_text(context_rows, source_text)
    failures, checked_docs = [], set()
    for source in sentence.sources:
        if not 0 < source <= len(context_rows):
            continue
        row = context_rows[source-1]
        if row['doc_id'] in checked_docs:
            continue
        checked_docs.add(row['doc_id'])
        cited = support.get(source, '')
        failure = quantity.verdict(asked, sentence.text, cited)
        if not failure:
            failure = quantity.binding_verdict(asked, sentence.text, cited, row['filename'])
        if not failure:
            return ''
        failures.append(failure)
    if 'quantity_role_unbound' in failures:
        return 'quantity_role_unbound'
    return next((f for f in failures if f != 'quantity_missing'), 'quantity_missing')


_CONFIRMED = "confirmed"
_JOSA_END = re.compile(r"(?:에서|으로|까지|부터|에게|은|는|이|가|을|를|의|에|로|도|과|와|만)$")


def _subject_unknown(db: Database, question: str, field_word: str,
                     scope: QueryScope | None = None) -> bool:
    """묻는 속성 바로 앞에 붙여 쓴 대상의 이름이 자료 전체에 한 번도 나오지 않는가.

    "정수장 소독약품 구매 단가는"에서 '단가' 앞의 '정수장·소독약품·구매'가 대상이다.
    조사 없이 이어 쓴 세 글자 이상의 낱말만 본다 — 조사가 붙은 말, 서술어, 꾸밈말은
    사람이 문서와 다르게 말하는 자리라 따지지 않는다('들어온', '배정된').
    조각 본문과 파일 이름 어디에도 없을 때만 참이다.
    """
    if not field_word:
        return False
    clause, params = (scope or QueryScope()).predicate()
    words = question.split()
    at = next((i for i, word in enumerate(words) if word.startswith(field_word)), None)
    if at is None:
        return False
    for word in reversed(words[:at]):
        if not re.fullmatch(r"[가-힣]{2,}", word) or _JOSA_END.search(word):
            break
        # 받침 ㄴ·ㄹ로 끝나는 말은 꾸밈말일 수 있다('매겨진', '배정된', '들어온', '쓸').
        # 이름인지 꾸밈말인지 가를 수 없으니 따지지 않는다.
        if len(word) < 3 or (ord(word[-1]) - 0xAC00) % 28 in (4, 8):
            continue
        like = f"%{word}%"
        if not db.con.execute(
                f"SELECT 1 FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE {clause} "
                "AND (c.text LIKE ? OR d.filename LIKE ?) LIMIT 1", (*params, like, like)).fetchone():
            return True
    return False


def _wrong_field(asked: "intent.QueryFrame", kept: list, context_rows: list,
                 source_text: dict[int, str]) -> list[str]:
    """문장마다의 판정. 다른 속성의 값이면 까닭(코드), 묻는 속성의 값임이 문서로
    확인되면 _CONFIRMED, 알 수 없으면 빈 문자열.

    문장이 인용한 문서의 글로 본다(같은 문서의 다른 조각 포함 — 검증과 같은 범위).
    """
    by_doc: dict[int, list[str]] = {}
    for index, row in enumerate(context_rows, start=1):
        by_doc.setdefault(row["doc_id"], []).append(source_text.get(index, ""))
    verdicts = []
    for sentence in kept:
        docs = {context_rows[source - 1]["doc_id"] for source in sentence.sources
                if 0 < source <= len(context_rows)}
        cited = "\n".join(text for doc in docs for text in by_doc[doc])
        verdict = intent.mismatch(asked, sentence.text, cited)
        if not verdict and intent.confirmed(asked, sentence.text, cited):
            verdict = _CONFIRMED
        verdicts.append(verdict)
    return verdicts


# 빈 문장 목록을 받았을 때 다시 묻는 데 쓴다. 문장을 하나 이상 내야 한다.
_ASK_SCHEMA_NONEMPTY = {
    **ASK_SCHEMA,
    "properties": {**ASK_SCHEMA["properties"],
                   "sentences": {**ASK_SCHEMA["properties"]["sentences"], "minItems": 1}},
}


def _ask_again(client, prompt: str, schema: dict):
    """같은 것을 한 번 더 묻는다. 씨앗을 고정해 두었으므로 씨앗을 바꿔서 묻는다 —
    그러지 않으면 같은 답이 그대로 돌아온다. 끝나면 씨앗을 되돌린다."""
    seed = getattr(client, "seed", None)
    if seed is not None:
        client.seed = seed + 1
    try:
        return client.generate_json(prompt, schema, num_predict=GEN_TOKEN_BUDGET)
    finally:
        if seed is not None:
            client.seed = seed


def _generation_record(client) -> dict:
    """클라이언트가 남긴 생성 계측값. 계측을 모르는 대역 클라이언트면 빈 값."""
    record = getattr(client, "last_generation", None)
    if record is None or not record.outcome:
        return {}
    if getattr(record, "prompt_truncated", False):
        # 모델이 지시문 없이 답했을 수 있다. 문장 검증은 그대로 하지만 원인을 남긴다.
        diagnostics.record("generation.prompt_truncated", count=record.prompt_eval_count)
    return record.as_dict()


_ANSWERED = re.compile(r'"answered"\s*:\s*(true|false)')
# 모델이 문장 안에 직접 적은 인용 표시. 우리가 다시 붙이기 전에 걷어 낸다.
_CITATION_MARK = re.compile(r"\s*\[\s*\d+(?:\s*,\s*\d+)*\s*\]")
_SENTENCE = re.compile(
    r'\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"sources"\s*:\s*\[([^\]]*)\]\s*\}'
)


def _salvage_ask_response(raw: str) -> dict | None:
    """토큰 한도에 걸려 잘린 JSON에서 완성된 문장만 건진다.

    Ollama의 format 강제로 파싱 실패는 드물지만, 문장 배열은 길어질수록
    한도를 넘길 위험이 커진다. 정규식으로 완성된 {"text":…,"sources":[…]}
    객체만 추려낸다 — 중간에 잘린 객체는 패턴에 안 맞아 자연히 빠진다.
    """
    answered_match = _ANSWERED.search(raw)
    if not answered_match:
        return None
    sentences = _complete_sentences(raw)
    if not sentences:
        return None
    return {"answered": answered_match.group(1) == "true", "sentences": sentences}


def _complete_sentences(raw: str) -> list[dict]:
    """끊긴 응답에서 끝까지 적힌 {"text":…,"sources":[…]} 항목만."""
    sentences: list[dict] = []
    for match in _SENTENCE.finditer(raw):
        try:
            text = json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            continue
        sources = [int(n) for n in re.findall(r"-?\d+", match.group(2))]
        sentences.append({"text": text, "sources": sources})
    return sentences


def _compose_answer(
    sentences: list[VerifiedSentence], context_rows: list,
    official: dict[int, dict] | None = None,
    evidence_text: dict[int, str] | None = None,
) -> tuple[str, list[Citation]]:
    """검증에서 살아남은 문장으로 답변 본문과 근거 목록을 만든다.

    원래 컨텍스트 번호([1]~[TOP_K])를 그대로 쓰지 않고, 실제로 쓰인 것만
    등장 순서대로 [1][2][3]으로 다시 매긴다 — 검증에서 몇 문장이 버려지면
    번호가 듬성듬성해지는데(예: [1][5][7]), 그대로 보여주면 "인용 전량
    나열" 문제(§1 측정 1)의 변형이 된다. 실제로 근거가 된 것만, 깔끔한
    번호로 보여준다.
    """
    by_index = {i + 1: row for i, row in enumerate(context_rows)}
    official = official or {}
    citations: list[Citation] = []
    renumbered: dict[int, int] = {}
    parts: list[str] = []

    for sentence in sentences:
        marks: list[int] = []
        for source in sentence.sources:
            row = by_index.get(source)
            if row is None:
                continue
            if source not in renumbered:
                renumbered[source] = len(citations) + 1
                citations.append(Citation(
                    index=renumbered[source], doc_id=row["doc_id"],
                    filename=row["filename"], locator=row["locator"], path=row["path"],
                    # 모델이 실제로 본 대목을 보여 준다 — 조각 앞머리가 아니라.
                    snippet=(evidence_text or {}).get(source)
                    or " ".join(row["text"].split())[:SNIPPET_CHARS],
                    official=provenance.describe(official.get(row["doc_id"])),
                    chunk_id=dict(row).get("chunk_id"),
                ))
            marks.append(renumbered[source])
        # 모델이 sources를 따로 내면서 문장 안에도 [1]을 적는 일이 잦다
        # (v2 프롬프트의 습관이 남아 있다). 그대로 두고 우리 번호를 붙이면
        # "…제출하였다[1][1]"이 된다. 문장에서 표시를 걷어 내고, 번호는
        # 검증을 통과한 sources로만 다시 매긴다 — 모델이 쓴 번호는 검증
        # 전 번호라 재정렬 뒤에는 어차피 틀린 값이다.
        body = _CITATION_MARK.sub("", sentence.text).rstrip()
        suffix = "".join(f"[{m}]" for m in sorted(set(marks)))
        parts.append(f"{body}{suffix}" if suffix else body)

    return " ".join(parts), citations


def _reciprocal_rank_fusion(
    vector_ranked: list[tuple[int, float]], fts_ranked: list[int], k: int = RRF_K,
) -> list[tuple[int, float]]:
    """의미 순위와 정확 일치 순위를 하나로 합친다.

    코사인 유사도와 FTS5 bm25는 값의 스케일이 전혀 다르다 — 0.4와 -12.3을
    직접 비교할 수 없다. RRF는 값이 아니라 순위(몇 번째로 좋은가)만 보고
    합치므로 스케일 문제가 애초에 생기지 않는다. 규칙만으로 되는 표준적인
    방법이라 생성 호출을 늘리지 않고도 두 검색을 결합할 수 있다.
    """
    scores: dict[int, float] = {}
    for rank, (chunk_id, _score) in enumerate(vector_ranked):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, chunk_id in enumerate(fts_ranked):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda item: -item[1])


# 바꿔 쓴 질의의 순위는 원래 질문의 절반만 친다. 원래 질문이 찾은 것을 밀어내지
# 않고 빈 곳을 채우는 것이 목적이다.
VARIANT_WEIGHT = 0.5
# 바꿔 쓴 질의의 정확 일치는 맨 위 몇 건만 '강한 근거'로 인정한다(OR 결합의 잡음).
VARIANT_FTS_STRONG = 3


def _with_variants(db, chunk_ids, matrix, variant_vectors, variants, vector_ranked, fts_ranked,
                   doc_ids) -> tuple[list[tuple[int, float]], set[int]]:
    """원래 질문과 바꿔 쓴 질의들의 순위를 RRF로 합친다. (합친 순위, 새로 강해진 조각)."""
    scores: dict[int, float] = {}

    def add(ranking: list[int], weight: float) -> None:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + rank + 1)

    add([cid for cid, _score in vector_ranked], 1.0)
    add(fts_ranked, 1.0)
    widened: set[int] = set()
    for vector, text in zip(variant_vectors, variants):
        similarity = matrix @ vector
        ranked = _parent_vector_rank(chunk_ids, similarity)
        add([cid for cid, _ in ranked], VARIANT_WEIGHT)
        widened |= {cid for cid, score in ranked if score >= MIN_SIMILARITY}
        exact = fts.search_chunk_ids(db, text, limit=RANK_POOL, doc_ids=doc_ids)
        add(exact, VARIANT_WEIGHT)
        widened |= set(exact[:VARIANT_FTS_STRONG])
    return sorted(scores.items(), key=lambda item: -item[1]), widened


def _select_context(
    db: Database, combined: list[tuple[int, float]], strong_ids: set[int], scope: QueryScope,
    question: str = "", trace: RetrievalTrace | None = None,
) -> list[tuple[int, float]]:
    """RRF 순위에서 실제로 컨텍스트에 넣을 TOP_K를 고른다.

    순서가 중요하다.
      ① 사용자 범위·업무·연도를 적용한 뒤 탐색 범위를 제한한다.
      ② 대표본 판정(파일 단위 중복 제거)
      ③ 내용 단위 근접 중복 축약 — 파일은 다른데 내용이 같은 버전 사본을
         접는다. 다양성 상한보다 **먼저** 해야 한다. 나중에 하면 같은 내용
         여덟 건이 각자 다른 문서라는 이유로 상한을 통과해 버린다.
      ④ 업무·연도 범위를 재확인한다
      ⑤ 규칙 재정렬 — 상한을 적용하기 전에 해야 좋은 후보가 살아남는다.
      ⑥ 문서당 상한 → TOP_K

    업무·연도 범위에 맞는 근거가 없으면 빈 결과를 반환한다.
    범위 밖 문서를 해당 업무·연도의 근거로 대체하지 않는다.
    """
    eligible = [(cid, score) for cid, score in combined if cid in strong_ids]
    # 質問がない旧APIでは、深い候補による穴埋めを引き続き禁じる。
    if not question:
        eligible = eligible[:EXPLORATION_WINDOW]
    if not eligible:
        return []

    meta = _load_chunk_meta(db, [cid for cid, _score in eligible])

    clause, params = scope.predicate()
    allowed = {r[0] for r in db.con.execute(f"SELECT d.id FROM documents d WHERE {clause}", params)}
    before_scope = len(eligible)
    eligible = [(cid, score) for cid, score in eligible if cid in meta and meta[cid]["doc_id"] in allowed]
    if trace:
        trace.record(scope_removed=before_scope-len(eligible), strong_candidates=before_scope,
                     window_removed=max(0, len(eligible)-EXPLORATION_WINDOW))
    eligible = eligible[:EXPLORATION_WINDOW]
    representative = ({cid for cid, _ in eligible} if scope.doc_ids is not None else
                      _representative_chunk_ids(db, [cid for cid, _score in eligible]))
    narrowed = [(cid, s) for cid, s in eligible if cid in representative]
    if narrowed:
        eligible = narrowed

    candidates = [
        Candidate(
            chunk_id=cid, score=score,
            text=meta.get(cid, {}).get("text", ""),
            filename=meta.get(cid, {}).get("filename", ""),
            eff_year=meta.get(cid, {}).get("eff_year"),
        )
        for cid, score in eligible
    ]
    collapsed = collapse_near_duplicates(candidates)
    if collapsed:
        candidates = collapsed

    if scope.has_task:
        in_task = _task_chunk_ids(db, scope.task_id, [c.chunk_id for c in candidates])
        matching = [c for c in candidates if c.chunk_id in in_task]
        candidates = matching

    if scope.has_year:
        matching = [c for c in candidates if c.eff_year in scope.years]
        candidates = matching

    # 연도는 위의 범위 필터가 이미 걸렀다 — 낱말 가산점에서는 뺀다.
    candidates = rerank(candidates, question, terms=_distinctive_terms(
        db, [term for term in korean.tokens(question) if not re.search(r"\d", term)], scope))
    if trace:
        trace.record(deduplicated_candidates=len(collapsed), reranked_candidates=len(candidates),
                     duplicate_removed=len(eligible)-len(collapsed))

    ranked = [(c.chunk_id, c.score) for c in candidates]
    doc_of = {cid: meta[cid]["doc_id"] for cid, _s in ranked if cid in meta}
    capped = _cap_per_document(ranked, doc_of)
    if trace:
        trace.record(diversity_removed=len(ranked)-len(capped), top_k_removed=max(0,len(capped)-TOP_K))
    return capped[:TOP_K]


def _cap_per_document(
    ranked: list[tuple[int, float]], doc_of: dict[int, int],
) -> list[tuple[int, float]]:
    """한 문서가 MAX_CHUNKS_PER_DOC를 넘겨 근거 자리를 차지하지 못하게 한다."""
    kept: list[tuple[int, float]] = []
    counts: dict[int, int] = {}
    for chunk_id, score in ranked:
        doc_id = doc_of.get(chunk_id)
        if doc_id is not None and counts.get(doc_id, 0) >= MAX_CHUNKS_PER_DOC:
            continue
        kept.append((chunk_id, score))
        if doc_id is not None:
            counts[doc_id] = counts.get(doc_id, 0) + 1
    return kept


def _task_chunk_ids(db: Database, task_id: int, chunk_ids: list[int]) -> set[int]:
    """그 업무에 배정된 문서의 조각만 남긴다.

    사용자 교정(task_docs.origin='user')도 그대로 반영된다 — 업무 화면에서
    문서를 옮겨 놓고 질문 화면에서 그 업무를 고르면 옮긴 대로 걸린다.
    """
    if not chunk_ids:
        return set()
    marks = ", ".join("?" * len(chunk_ids))
    rows = db.con.execute(
        f"SELECT c.id AS chunk_id FROM chunks c "
        f"JOIN task_docs td ON td.doc_id = c.doc_id "
        f"WHERE td.task_id = ? AND c.id IN ({marks})",
        (task_id, *chunk_ids),
    ).fetchall()
    return {row["chunk_id"] for row in rows}


def _distinctive_terms(db: Database, terms: list[str], scope: QueryScope | None = None) -> list[str]:
    """질문 낱말 중 실제로 주제를 가르는 것만 남긴다.

    "계약관리 업무는 어떤 순서로 처리해?"에서 주제를 가르는 낱말은
    '계약관리'뿐이다. '업무'는 이 코퍼스의 거의 모든 문서에 들어 있어
    아무것도 가르지 못한다 — 그런 낱말로 관련성을 재면 무엇이든 통과한다.
    실측으로 겪었다: 무관한 월간실적보고 문단이 '업무' 하나로 이웃 확장을
    통과해, 모델이 그 표 숫자로 계약관리 질문에 답해 버렸다.

    문서 빈도로 가른다 — 절반 넘는 문서에 나오는 낱말은 버린다. 전부
    흔한 낱말뿐이면 원래 목록을 그대로 쓴다(가릴 근거가 없으면 막지 않는다).
    """
    if not terms:
        return []
    clause, params = (scope or QueryScope()).predicate()
    total = db.con.execute(
        f"SELECT COUNT(DISTINCT c.doc_id) AS n FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE {clause}",
        params).fetchone()["n"]
    if not total:
        return terms

    distinctive = []
    for term in terms:
        # 대조와 같은 어간으로 센다. 조사가 붙은 "업무는"으로 세면 드물다고
        # 나오지만, 실제로 대조에 쓰이는 "업무"는 어느 공문에나 있다.
        hits = db.con.execute(
            f"SELECT COUNT(DISTINCT c.doc_id) AS n FROM chunks c JOIN documents d ON d.id=c.doc_id "
            f"WHERE {clause} AND c.text LIKE ?",
            (*params, f"%{korean.stem(term)}%"),
        ).fetchone()["n"]
        if hits <= total * GENERIC_TERM_RATIO:
            distinctive.append(term)
    return distinctive or terms


def _support_text(context_rows: list, source_text: dict[int, str]) -> dict[int, str]:
    """사실 확인에 쓸 본문 — 인용한 조각 + **같은 문서의** 다른 컨텍스트 조각.

    이웃 확장으로 넣어 준 조각을 검증에서 근거로 인정하기 위한 것이다.
    같은 문서로 한정하는 것이 핵심이다 — 컨텍스트 전체를 허용하면 A 문서를
    인용하고 B 문서의 숫자를 쓴 문장까지 통과해 검증이 무의미해진다.
    """
    by_document: dict[int, list[str]] = {}
    for index, row in enumerate(context_rows, start=1):
        by_document.setdefault(row["doc_id"], []).append(source_text[index])
    return {
        index: "\n".join(by_document[row["doc_id"]])
        for index, row in enumerate(context_rows, start=1)
    }


def _expand_neighbors(db: Database, chunk_ids: list[int], question: str = "",
                      scope: QueryScope | None = None) -> list[int]:
    """고른 조각의 바로 앞뒤 조각을 함께 넣는다. 원래 순위 순서는 유지한다.

    조각 하나가 스스로를 설명하지 못할 때가 있다 — 표에는 숫자만 있고
    그 표가 무엇에 관한 것인지는 앞 문단에만 적혀 있다. 이웃을 붙이면
    모델이 표의 숫자를 질문과 이어 붙일 수 있다.

    **질문과 무관한 이웃은 붙이지 않는다.** 이웃 확장은 관련 있는 근거를
    설명 가능하게 만드는 장치이지, 무관한 근거를 풍부해 보이게 하는 것이
    아니다. 실측으로 겪었다 — "계약관리 처리 순서"에 월간실적보고의 표가
    걸렸을 때, 그 표만 있으면 모델이 "자료가 부족하다"고 옳게 유보했는데
    이웃 문단을 붙여 주자 그 숫자로 답을 만들어 냈다.

    이웃은 원래 조각 바로 뒤에 놓는다. 앞에 두면 컨텍스트 번호([1],[2]…)가
    검색 순위와 어긋나 모델이 "가장 관련 있는 것부터"라는 감을 잃는다.
    """
    if not chunk_ids:
        return []
    terms = _distinctive_terms(db, korean.tokens(question), scope)

    marks = ", ".join("?" * len(chunk_ids))
    clause, params = (scope or QueryScope()).predicate()
    anchors = db.con.execute(
        f"SELECT c.id, c.doc_id, c.ordinal FROM chunks c JOIN documents d ON d.id=c.doc_id "
        f"WHERE c.id IN ({marks}) AND {clause}", (*chunk_ids, *params)).fetchall()
    allowed_anchors = {row['id'] for row in anchors}
    chunk_ids = [cid for cid in chunk_ids if cid in allowed_anchors]
    position = {row["id"]: (row["doc_id"], row["ordinal"]) for row in anchors}

    wanted: list[tuple[int, int]] = []
    for chunk_id in chunk_ids:
        if chunk_id not in position:
            continue
        doc_id, ordinal = position[chunk_id]
        for offset in range(-NEIGHBOR_SPAN, NEIGHBOR_SPAN + 1):
            if offset:
                wanted.append((doc_id, ordinal + offset))
    if not wanted:
        return chunk_ids

    conditions = " OR ".join("(doc_id = ? AND ordinal = ?)" for _ in wanted)
    params = [value for pair in wanted for value in pair]
    found = db.con.execute(
        f"SELECT id, doc_id, ordinal, text FROM chunks WHERE {conditions}", params
    ).fetchall()
    by_position = {
        (row["doc_id"], row["ordinal"]): row["id"]
        for row in found
        if not terms or any(korean.contains(row["text"], t) for t in terms)
    }

    named_rows = _named_table_rows(db, {doc for doc, _ordinal in position.values()}, question)

    out: list[int] = []
    seen: set[int] = set()
    for chunk_id in chunk_ids:
        if chunk_id not in seen:
            out.append(chunk_id)
            seen.add(chunk_id)
        if chunk_id not in position:
            continue
        doc_id, ordinal = position[chunk_id]
        for offset in range(-NEIGHBOR_SPAN, NEIGHBOR_SPAN + 1):
            if not offset:
                continue
            neighbour = by_position.get((doc_id, ordinal + offset))
            if neighbour is not None and neighbour not in seen:
                out.append(neighbour)
                seen.add(neighbour)
        for row_chunk in named_rows.get(doc_id, []):
            if row_chunk not in seen:
                out.append(row_chunk)
                seen.add(row_chunk)
    return out


def _named_table_rows(db: Database, doc_ids: set[int], question: str) -> dict[int, list[int]]:
    """고른 문서의 표에서 질문이 이름을 부른 행의 조각. 문서마다 TABLE_ROWS_PER_DOC개까지.

    한글·ODT 표는 행마다 조각이 따로다. 제목 문단이 걸려도 바로 옆 조각은 표의 머리
    행이고, 질문이 묻는 행("운영비")은 두세 조각 뒤에 있어 이웃 확장이 닿지 않는다 —
    표를 두고도 "자료가 부족하다"로 끝났다(다른 말로 묻는 24문항).
    행 이름이 질문에 글자 그대로 있을 때만 넣는다.
    """
    if not doc_ids:
        return {}
    squeezed = re.sub(r"\s+", "", question)
    marks = ", ".join("?" * len(doc_ids))
    rows = db.con.execute(
        f"SELECT id, doc_id, text FROM chunks WHERE doc_id IN ({marks}) "
        f"AND text LIKE '%1열:%' ORDER BY doc_id, ordinal", sorted(doc_ids)).fetchall()
    found: dict[int, list[int]] = {}
    for row in rows:
        label = _ROW_LABEL.search(row["text"])
        name = re.sub(r"\s+", "", label.group(1)) if label else ""
        if len(name) >= 2 and name in squeezed and len(found.get(row["doc_id"], [])) < TABLE_ROWS_PER_DOC:
            found.setdefault(row["doc_id"], []).append(row["id"])
    return found


_ROW_LABEL = re.compile(r"(?m)^\s*1열:\s*([^|\n]+?)\s*(?:\||$)")
TABLE_ROWS_PER_DOC = 2


def _representative_chunk_ids(db: Database, chunk_ids: list[int]) -> set[int]:
    """완전 중복 문서 중 대표본에 속한 조각만 남긴다.

    When·How·문서 화면과 같은 판정 기준(db.representative_predicate)을
    쓴다 — 화면마다 다른 문서를 대표로 고르면 사용자가 혼란스럽다.
    """
    if not chunk_ids:
        return set()
    marks = ", ".join("?" * len(chunk_ids))
    rows = db.con.execute(
        f"SELECT c.id AS chunk_id FROM chunks c JOIN documents d ON d.id = c.doc_id "
        f"WHERE c.id IN ({marks}) AND {representative_predicate('d')}",
        chunk_ids,
    ).fetchall()
    return {row["chunk_id"] for row in rows}


def _load_chunk_meta(db: Database, chunk_ids: list[int]) -> dict[int, dict]:
    if not chunk_ids:
        return {}
    marks = ", ".join("?" * len(chunk_ids))
    rows = db.con.execute(
        f"SELECT c.id AS chunk_id, c.doc_id, c.text, d.eff_year, d.filename "
        f"FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({marks})",
        chunk_ids,
    ).fetchall()
    return {
        row["chunk_id"]: {
            "doc_id": row["doc_id"], "eff_year": row["eff_year"],
            "text": row["text"], "filename": row["filename"],
        }
        for row in rows
    }


def _parent_vector_rank(ids, scores) -> list[tuple[int, float]]:
    """자식 최고 점수로 부모를 한 번만 순위화한다. 긴 문서에 중복 가산하지 않는다."""
    best = {}
    for cid, score in zip(ids, scores):
        key = int(cid)
        best[key] = max(best.get(key, float('-inf')), float(score))
    return sorted(best.items(), key=lambda item: (-item[1], item[0]))[:RANK_POOL]


def _load_chunk_matrix(db: Database, model: str, doc_ids=None) -> tuple[np.ndarray, np.ndarray]:
    restriction = ""
    args = []
    if doc_ids is not None:
        restriction = " AND d.id IN (" + (",".join("?" for _ in doc_ids) or "NULL") + ")"
        args = list(doc_ids)
    rows = db.con.execute(
        "WITH ready AS (SELECT s.parent_id FROM chunk_children s "
        "LEFT JOIN child_embeddings e ON e.child_id=s.id AND e.model=? "
        "GROUP BY s.parent_id HAVING COUNT(*)=COUNT(e.child_id) AND MIN(e.dim)=MAX(e.dim)), "
        "vectors AS (SELECT s.parent_id AS chunk_id,e.dim,e.vector FROM child_embeddings e "
        "JOIN chunk_children s ON s.id=e.child_id JOIN ready r ON r.parent_id=s.parent_id WHERE e.model=? "
        "UNION ALL SELECT e.chunk_id,e.dim,e.vector FROM embeddings e WHERE e.model=? "
        "AND NOT EXISTS(SELECT 1 FROM ready r WHERE r.parent_id=e.chunk_id)) "
        "SELECT e.chunk_id, e.dim, e.vector FROM vectors e "
        "JOIN chunks c ON c.id = e.chunk_id "
        "JOIN documents d ON d.id = c.doc_id "
        "WHERE d.missing_since IS NULL" + restriction,
        (model, model, model, *args),
    ).fetchall()
    if not rows:
        return np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=np.float32)

    dim = rows[0]["dim"]
    usable = [r for r in rows if r["dim"] == dim]
    ids = np.fromiter((r["chunk_id"] for r in usable), dtype=np.int64, count=len(usable))
    matrix = np.vstack([unpack(r["vector"]) for r in usable]).astype(np.float32, copy=False)
    return ids, normalize(matrix)


def _load_chunk_context(db: Database, chunk_ids: list[int]) -> list:
    if not chunk_ids:
        return []
    marks = ", ".join("?" * len(chunk_ids))
    rows = db.con.execute(
        f"SELECT c.id AS chunk_id, c.doc_id, c.locator, c.text, c.context, d.filename, d.path "
        f"FROM chunks c JOIN documents d ON d.id = c.doc_id "
        f"WHERE c.id IN ({marks}) AND d.missing_since IS NULL",
        chunk_ids,
    ).fetchall()
    order = {cid: i for i, cid in enumerate(chunk_ids)}
    return sorted(rows, key=lambda r: order.get(r["chunk_id"], len(chunk_ids)))


def _context_budget(count: int) -> int:
    """조각 하나에 쓸 글자 수. 근거가 적으면 넉넉히, 많으면 예전만큼.

    전체 문맥은 예전의 최악(조각마다 400자)을 넘지 않는다 — 근거가 서너
    건뿐일 때 조각을 통째로 보여 줄 여유가 있는데도 400자에서 자르던
    것만 고친다.
    """
    return max(MAX_CONTEXT_CHARS, min(MAX_CHUNK_CHARS, CONTEXT_TOTAL_CHARS // max(count, 1)))


def _evidence_text(rows, question: str) -> dict[int, str]:
    """{근거 번호: 모델에 보여 줄 글}. 번호는 _format_context와 같다."""
    budget = _context_budget(len(rows))
    return {i: evidence.window(row["text"], question, budget) for i, row in enumerate(rows, start=1)}


def context_head(row) -> str:
    """근거 머리줄 — 파일·위치와, 알고 있으면 그 대목이 속한 제목. 제목은 모델이 대목의
    자리를 알게 하는 단서일 뿐 근거 본문이 아니다(검증은 본문으로만 한다)."""
    head = f"{row['filename']} · {row['locator']}"
    try:
        context = row["context"]
    except (IndexError, KeyError):
        context = ""
    return f"{head} · 제목: {context}" if context else head


def _format_context(rows, official: dict[int, dict] | None = None,
                    evidence_text: dict[int, str] | None = None) -> str:
    """공식 출처가 있으면 머리줄에 함께 적는다.

    등록본과 초안이 같은 주제로 함께 걸렸을 때 모델이 둘을 구분할 단서다.
    상태만으로 어느 쪽이 맞는지 정하라는 뜻은 아니다 — 충돌은 그대로 드러낸다.
    """
    official = official or {}
    parts = []
    for i, row in enumerate(rows, start=1):
        text = (evidence_text or {}).get(i) or " ".join(row["text"].split())[:MAX_CONTEXT_CHARS]
        head = context_head(row)
        label = provenance.describe(official.get(row["doc_id"]))
        if label:
            head += f" · {label}"
        parts.append(f"[{i}] ({head})\n{text}")
    return "\n\n".join(parts)


def _related_documents(db: Database, chunk_ids: list[int]) -> list[RelatedDoc]:
    rows = _load_chunk_context(db, chunk_ids)
    seen: list[RelatedDoc] = []
    known: set[int] = set()
    for row in rows:
        if row["doc_id"] in known:
            continue
        known.add(row["doc_id"])
        seen.append(RelatedDoc(row["doc_id"], row["filename"], row["path"]))
    return seen


def _dedupe(items: list[RelatedDoc]) -> list[RelatedDoc]:
    out: list[RelatedDoc] = []
    seen: set[int] = set()
    for item in items:
        if item.doc_id in seen:
            continue
        seen.add(item.doc_id)
        out.append(item)
    return out
