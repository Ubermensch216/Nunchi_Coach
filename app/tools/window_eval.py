"""부모·자식 창 크기 비교 (업무노트 RAG 2단계) — 검색과 문맥 구성까지만 잰다.

    python -m app.tools.window_eval --folder .test_runs/window-eval --json .test_runs/window-eval.json

같은 합성 자료와 같은 질문을 창 크기만 바꿔 색인하고, 앱이 쓰는 검색 함수(자식 글자
검색 + 자식 벡터 → 부모 복원 → RRF)로 찾는다. 답을 생성하지 않으므로 생성 모델은
쓰지 않는다 — 임베딩 모델만 있으면 된다.

재는 것
  hit@k    정답 값이 적힌 부모 조각이 상위 k 안에 있는 질문의 비율
  mrr      그 조각의 순위 역수 평균(8위 밖은 0)
  context  상위 8개를 질문 관련 대목으로 줄인 뒤(search/rag.py _evidence_text) 정답 값이
           모델에게 보이는 글 안에 남아 있는 비율. 찾았는데 잘려서 못 보는 경우를 가른다.

'자식 없음'은 제목 문맥도 검색에 쓰지 못한다(문맥은 자식의 검색문에 붙는다). 자식 창과
제목 문맥의 몫을 가르려고 '제목 문맥 없음' 설정을 따로 둔다.

한계: 자료와 질문은 이 도구가 만든 합성물이다. 문서 12건·질문 72개이고, 실제 기관
문서의 문체·표 모양·오탈자를 닮지 않았다. 창 크기를 고르는 근거로는 쓰되 실제 정확도로
읽지 않는다. 답변 생성과 근거 검증은 여기서 재지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..ai.client import OllamaClient
from ..core import child_chunking
from ..core.chunking import (MAX_CHUNK_CHARS, TARGET_MAX_CHARS, TARGET_MIN_CHARS,
                             build_chunks_with_context)
from ..db import Database
from ..search import fts, rag
from ..search.vector import normalize, pack
from . import console

TOP = rag.TOP_K
EMBED_BATCH = 16


@dataclass(frozen=True, slots=True)
class Config:
    name: str
    target_min: int
    target_max: int
    max_chars: int
    child: int | None      # None이면 자식 없이 부모 벡터로 찾는다(2단계 이전 방식)
    context: bool = True   # 제목 문맥을 조각에 남기는가. 끄면 자식 창의 효과만 남는다


CONFIGS = (
    Config("부모 400~800/1200 · 자식 없음", TARGET_MIN_CHARS, TARGET_MAX_CHARS, MAX_CHUNK_CHARS, None),
    Config("부모 400~800/1200 · 자식 500 · 제목 문맥 없음", TARGET_MIN_CHARS, TARGET_MAX_CHARS, MAX_CHUNK_CHARS,
           500, context=False),
    Config("부모 400~800/1200 · 자식 250", TARGET_MIN_CHARS, TARGET_MAX_CHARS, MAX_CHUNK_CHARS, 250),
    Config("부모 400~800/1200 · 자식 350", TARGET_MIN_CHARS, TARGET_MAX_CHARS, MAX_CHUNK_CHARS, 350),
    Config("부모 400~800/1200 · 자식 500", TARGET_MIN_CHARS, TARGET_MAX_CHARS, MAX_CHUNK_CHARS, 500),
    Config("부모 300~600/800 · 자식 250", 300, 600, 800, 250),
    Config("부모 300~600/800 · 자식 500", 300, 600, 800, 500),
    Config("부모 600~1200/1600 · 자식 250", 600, 1200, 1600, 250),
    Config("부모 600~1200/1600 · 자식 500", 600, 1200, 1600, 500),
)

# ── 합성 자료 ───────────────────────────────────────────────────────
PROGRAMS = ("지역아동센터 운영", "노인일자리 지원", "청년 창업 공간", "마을도서관 운영", "방과후 돌봄교실",
            "장애인 활동지원", "다문화가족 교육", "농촌 일손돕기", "전통시장 환경개선", "주민자치 프로그램",
            "공공체육시설 관리", "어린이 급식관리")
CENTERS = ("새봄", "한울", "푸른", "다솜", "누리", "가온", "미르", "나래", "도담", "라온", "마루", "바른",
           "소망", "아름", "늘봄", "해든", "솔빛", "온새", "하람", "채움", "보람", "슬기", "이든", "별하")
DEPARTMENTS = ("복지정책과", "노인장애인과", "일자리경제과", "평생교육과", "교육지원과", "장애인복지과",
               "가족정책과", "농업정책과", "지역경제과", "자치행정과", "체육진흥과", "위생정책과")
FILLER = ("사업 수행기관은 분기마다 운영 실적을 점검하고 그 결과를 내부 결재로 남긴다.",
          "이용자 만족도 조사는 하반기에 한 차례 하며 결과는 다음 연도 계획에 반영한다.",
          "시설 안전점검표는 매월 작성하고 지적 사항은 조치 결과와 함께 보관한다.",
          "프로그램 참여자 명단은 개인정보 보호 지침에 따라 잠금 장치가 있는 곳에 둔다.",
          "운영위원회 회의록은 회의가 끝난 뒤 일주일 안에 작성해 위원에게 회람한다.",
          "홍보물은 기관 누리집과 게시판에 올리고 배포 수량을 기록해 둔다.",
          "종사자 교육 이수 여부는 연말에 확인하고 미이수자는 다음 차수에 배정한다.",
          "물품을 살 때는 견적서를 두 곳 이상 받아 비교하고 검수 조서를 남긴다.")


def _filler(seed: int, sentences: int) -> str:
    return " ".join(FILLER[(seed + i) % len(FILLER)] for i in range(sentences))


def corpus() -> tuple[list[dict], list[dict]]:
    """문서(파서가 낸 것과 같은 꼴의 섹션들)와 질문(정답 값·정답 문서)."""
    documents, questions = [], []
    for n, program in enumerate(PROGRAMS):
        year = 2026
        total = 420000 + n * 37100
        deadline = f"{year + 1}. 1. {12 + n}."
        refund = f"{year + 1}. 2. {9 + n}."
        graduates = 31 + n * 7
        staff = 2 + n % 5
        sections: list[tuple] = []

        def add(kind, text, heading, **meta):
            locator = f"{len(sections) + 1}문단" if kind == "paragraph" else meta.pop("locator")
            sections.append((kind, locator, text, {"heading": heading, **meta}))

        top = f"{year}년 {program} 사업 보조금 정산 안내"
        add("paragraph", top, [top])
        add("paragraph", "1. 사업 개요", [top, "1. 사업 개요"])
        add("paragraph", f"이 문서는 {year}년도 {program} 사업의 보조금 집행 내역을 확인하고 정산 절차를 안내한다. "
            + _filler(n, 3), [top, "1. 사업 개요"])
        add("paragraph", f"{program} 사업의 담당 부서는 {DEPARTMENTS[n]}이며 실무 담당 인원은 {staff}명이다. "
            + _filler(n + 2, 2), [top, "1. 사업 개요"])
        add("paragraph", _filler(n + 4, 6), [top, "1. 사업 개요"])

        add("paragraph", "2. 교육 운영 결과", [top, "2. 교육 운영 결과"])
        add("paragraph", _filler(n + 1, 5) + f" 올해 {program} 종사자 교육의 수료 인원은 {graduates}명이고 "
            f"교육은 모두 {3 + n % 4}회 열렸다. " + _filler(n + 5, 5), [top, "2. 교육 운영 결과"])

        add("paragraph", "3. 수행기관별 집행 현황", [top, "3. 수행기관별 집행 현황"])
        header = "수행기관\t교부액(천원)\t집행액(천원)\t집행잔액(천원)"
        rows = []
        for i, center in enumerate(CENTERS):
            granted = 21000 + n * 900 + i * 1370
            spent = granted - (310 + n * 13 + i * 29)
            rows.append(f"{center}센터\t{granted:,}\t{spent:,}\t{granted - spent:,}")
        rows.append(f"합계\t{total:,}\t{total - 21900:,}\t21,900")
        buffer, first = [header], 1
        for number, row in enumerate(rows, start=2):
            if sum(map(len, buffer)) + len(buffer) + len(row) > 900:
                add("table", "\n".join(buffer), [top, "3. 수행기관별 집행 현황"], table=1, header=header,
                    rows=[first, number - 1], locator=f"표1 · {first}~{number - 1}행")
                buffer, first = [header], number
            buffer.append(row)
        add("table", "\n".join(buffer), [top, "3. 수행기관별 집행 현황"], table=1, header=header,
            rows=[first, len(rows) + 1], locator=f"표1 · {first}~{len(rows) + 1}행")

        add("paragraph", "4. 집행잔액 반납", [top, "4. 집행잔액 반납"])
        add("paragraph", _filler(n + 3, 4) + f" 남은 보조금과 이자는 {refund}까지 지정 계좌로 돌려보낸 뒤 "
            f"입금 확인서를 {DEPARTMENTS[n]}에 낸다. " + _filler(n + 6, 4), [top, "4. 집행잔액 반납"])

        # 쪽 단위로 읽힌 긴 붙임(PDF처럼 한 섹션이 길다).
        sections.append(("page", "붙임 1쪽", _filler(n, 14) + f" {program} 사업의 정산서와 증빙서류는 {deadline}까지 "
                         f"{DEPARTMENTS[n]}로 제출한다. " + _filler(n + 3, 14), {"heading": [top, "붙임 유의사항"]}))
        sections.append(("page", "붙임 2쪽", _filler(n + 2, 30), {"heading": [top, "붙임 유의사항"]}))

        documents.append({"filename": f"{year}_{program.replace(' ', '')}_정산안내.docx", "sections": sections})
        middle = CENTERS[13 + n % 8]      # 표의 뒤쪽 조각에 있는 행
        granted = 21000 + n * 900 + CENTERS.index(middle) * 1370
        for text, gold in (
                (f"{program} 사업 정산서 제출기한은 언제야?", deadline),
                (f"{program} 사업에서 {middle}센터의 교부액은 얼마야?", f"{granted:,}"),
                (f"{program} 사업 담당 부서는 어디야?", f"담당 부서는 {DEPARTMENTS[n]}"),
                (f"{program} 종사자 교육 수료 인원은 몇 명이야?", f"수료 인원은 {graduates}명"),
                (f"{program} 집행잔액 반납은 언제까지 해야 해?", refund),
                (f"{program} 사업 교부액 합계는 얼마야?", f"{total:,}")):
            questions.append({"question": text, "gold": gold, "doc": n})
    return documents, questions


# ── 색인 ────────────────────────────────────────────────────────────
class Embedder:
    """같은 글은 한 번만 임베딩한다. 설정마다 겹치는 조각이 많다."""

    def __init__(self, client, cache_path: Path):
        self.client, self.path = client, cache_path
        self.cache: dict[str, list[float]] = {}
        self.seconds, self.calls = 0.0, 0
        if cache_path.is_file():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def vectors(self, texts: list[str]) -> list[list[float]]:
        keys = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
        missing = [(key, text) for key, text in dict(zip(keys, texts)).items() if key not in self.cache]
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start:start + EMBED_BATCH]
            started = time.monotonic()
            vectors, error = self.client.embed([text for _key, text in batch])
            self.seconds += time.monotonic() - started
            self.calls += len(batch)
            if error or not vectors:
                raise RuntimeError(f"임베딩 실패: {error}")
            for (key, _text), vector in zip(batch, vectors):
                self.cache[key] = vector
        if missing:
            self.path.write_text(json.dumps(self.cache), encoding="utf-8")
        return [self.cache[key] for key in keys]


def build(folder: Path, config: Config, documents: list[dict], embedder: Embedder) -> tuple[Database, list[int], dict]:
    path = folder / (hashlib.sha256(config.name.encode("utf-8")).hexdigest()[:10] + ".db")
    for stale in folder.glob(path.name + "*"):
        stale.unlink()
    db = Database(path)
    db.init()
    source = db.add_source(folder / "합성자료")
    doc_ids, parents, children = [], [], []
    model = embedder.client.embed_model
    child_chunking.CHILD_CHARS = config.child or child_chunking.CHILD_CHARS   # 이 도구에서만 바꾼다
    for number, document in enumerate(documents, start=1):
        doc = db.upsert_document(source, {
            "path": str(folder / "합성자료" / document["filename"]), "filename": document["filename"],
            "ext": ".docx", "parse_status": "ok", "hash": f"synthetic-{number}"})
        doc_ids.append(doc)
        db.replace_sections(doc, [(kind, i, locator, text, meta)
                                  for i, (kind, locator, text, meta) in enumerate(document["sections"], start=1)])
        sections = document["sections"] if config.context else [s[:3] for s in document["sections"]]
        pieces = build_chunks_with_context(sections, target_min=config.target_min,
                                           target_max=config.target_max, max_chars=config.max_chars)
        made = db.replace_document_chunks(doc, pieces)
        parents += [len(text) for _id, text in made]
        if config.child is None:
            for (chunk_id, text), vector in zip(made, embedder.vectors([text for _id, text in made])):
                db.save_chunk_embedding(chunk_id, model, len(vector), pack(vector))
            continue
        db.ensure_child_chunks(doc)
        pending = db.children_needing_embedding(doc, model)
        children += [len(text) for _id, text in pending]
        for (child_id, _text), vector in zip(pending, embedder.vectors([text for _id, text in pending])):
            db.save_child_embedding(child_id, model, len(vector), pack(vector))
    db.con.commit()
    shape = {"parents": len(parents), "parent_chars_mean": round(sum(parents) / len(parents)),
             "parent_chars_max": max(parents), "children": len(children),
             "child_chars_mean": round(sum(children) / len(children)) if children else 0}
    return db, doc_ids, shape


# ── 검색과 채점 ─────────────────────────────────────────────────────
def measure(db: Database, doc_ids: list[int], questions: list[dict], embedder: Embedder) -> dict:
    model = embedder.client.embed_model
    chunk_ids, matrix = rag._load_chunk_matrix(db, model)
    queries = normalize(np.asarray(embedder.vectors([q["question"] for q in questions]), dtype=np.float32))
    ranks, visible, misses = [], 0, []
    for question, vector in zip(questions, queries):
        vector_ranked = rag._parent_vector_rank(chunk_ids, matrix @ vector)
        fts_ranked = fts.search_chunk_ids(db, question["question"], limit=rag.RANK_POOL)
        combined = [cid for cid, _score in rag._reciprocal_rank_fusion(vector_ranked, fts_ranked)][:TOP]
        rows = rag._load_chunk_context(db, combined)
        gold_doc = doc_ids[question["doc"]]
        rank = next((i for i, row in enumerate(rows, start=1)
                     if row["doc_id"] == gold_doc and question["gold"] in row["text"]), 0)
        ranks.append(rank)
        if rank:
            window = rag._evidence_text(rows, question["question"])
            visible += question["gold"] in window[rank]
        else:
            misses.append(question["question"])
    count = len(questions)
    return {
        "hit@1": round(sum(r == 1 for r in ranks) / count, 4),
        "hit@3": round(sum(0 < r <= 3 for r in ranks) / count, 4),
        f"hit@{TOP}": round(sum(r > 0 for r in ranks) / count, 4),
        "mrr": round(sum(1 / r for r in ranks if r) / count, 4),
        "context": round(visible / count, 4),
        "missed": misses,
    }


def main(argv: list[str] | None = None) -> int:
    console.force_utf8()
    parser = argparse.ArgumentParser(description="부모·자식 창 크기별 검색·문맥 비교(합성 자료)")
    parser.add_argument("--folder", type=Path, required=True, help="비교용 프로젝트를 만들 빈 폴더")
    parser.add_argument("--json", type=Path, help="결과를 남길 파일")
    args = parser.parse_args(argv)
    args.folder.mkdir(parents=True, exist_ok=True)
    client = OllamaClient()
    if not client.health().embedding_ready:
        print("임베딩 모델이 준비되지 않았습니다.")
        return 2
    default_child = child_chunking.CHILD_CHARS
    documents, questions = corpus()
    embedder = Embedder(client, args.folder / "embeddings.json")
    results = []
    try:
        for config in CONFIGS:
            db, doc_ids, shape = build(args.folder, config, documents, embedder)
            try:
                scores = measure(db, doc_ids, questions, embedder)
            finally:
                db.close()
            results.append({"config": config.name, **shape, **scores})
            print(f"{config.name:<40} 부모 {shape['parents']:>3}(평균 {shape['parent_chars_mean']:>4}자) "
                  f"자식 {shape['children']:>4} | hit@1 {scores['hit@1']:.3f} hit@3 {scores['hit@3']:.3f} "
                  f"hit@{TOP} {scores[f'hit@{TOP}']:.3f} mrr {scores['mrr']:.3f} 문맥 {scores['context']:.3f}",
                  flush=True)
    finally:
        child_chunking.CHILD_CHARS = default_child
    report = {"documents": len(documents), "questions": len(questions), "embed_model": client.embed_model,
              "embedded_texts": embedder.calls, "embed_seconds": round(embedder.seconds, 1),
              "top_k": TOP, "results": results}
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"문서 {len(documents)}건 · 질문 {len(questions)}개 · 새로 임베딩한 글 {embedder.calls:,}개 "
          f"({embedder.seconds:.0f}초)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
