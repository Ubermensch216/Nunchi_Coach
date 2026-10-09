"""처리 파이프라인 — 찾기 → 해시 → 내용 읽기.

재개는 별도 큐가 아니라 **문서의 상태 자체**로 이뤄진다. `hash IS NULL`이면
아직 해시 전이고 `parse_status='pending'`이면 아직 안 읽은 것이다. 앱이 강제
종료돼도 다음 실행에서 같은 질의가 남은 일을 그대로 집어낸다(ING-006).

jobs 테이블은 재시도 횟수를 세는 데만 쓴다. 실패한 파일을 재시작할 때마다
무한히 다시 시도하면 영원히 끝나지 않기 때문이다.

원본은 읽기만 한다. 이 계층에도 쓰기 연산이 없다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal

from ..ai import OllamaClient
from ..ai.settings import project_client
from ..ai.discover import discover
from ..core import dating, handover_release, reconstruction, timeline, transfer
from ..core import diagnostics
from ..core.chunking import build_chunks
from ..core.labeling import label_document
from ..core.workcard import OUTPUT_DIR
from ..db import Database
from ..ingest import scanner
from ..search import pack
from ..ingest.hasher import sha256
from ..ingest.parsers import parse
from ..ingest.parsers.base import LOCKED, PARSER_VERSION, ParseResult

MAX_ATTEMPTS = 3

# 잠긴 파일 재시도. 짧게 여러 번 — 잠김은 대개 순간적이다.
LOCK_RETRIES = 3
LOCK_RETRY_DELAY = 0.3
COMMIT_EVERY = 50

STAGE_SCAN = "파일 찾기"
STAGE_HASH = "중복 확인"
STAGE_PARSE = "내용 읽기"
STAGE_DATE = "시점 확인"
STAGE_EMBED = "의미 색인"
STAGE_DISCOVER = "업무 파악"
STAGE_CYCLES = "일정 파악"
STAGE_STEPS = "처리 순서 파악"
STAGE_RESTORE = "업무 복원"
STAGE_CHUNKS = "질문 준비"
CHUNK_EMBED_BATCH = 32

# 임베딩에 쓸 글자 수. 파일명과 앞부분만으로도 문서의 정체는 거의 드러난다.
EMBED_CHARS = 1200
EMBED_BATCH = 32

# 시점 추정에 쓸 앞쪽 조각 수. 문서 자신의 날짜는 표지·머리말에 있다.
DATE_SECTIONS = 6


@dataclass
class StageReport:
    stage: str
    done: int = 0
    total: int = 0
    note: str = ""
    errors: list[str] = field(default_factory=list)


class Pipeline(QObject):
    """워커 스레드에서 도는 처리 본체.

    DB 연결은 스레드마다 따로 열어야 하므로 경로만 받아 여기서 연다.
    """

    progress = Signal(str, int, int, str)   # stage, done, total, note
    stage_done = Signal(object)             # StageReport
    finished = Signal()
    failed = Signal(str)

    def __init__(self, db_path: Path | str, parent: QObject | None = None):
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._stop = False
        self._client = None

    def stop(self) -> None:
        self._stop = True
        if self._client is not None:
            self._client.cancel()

    def _stopped(self) -> bool:
        return self._stop

    # ── 실행 ────────────────────────────────────────────────────────
    def run(self) -> None:
        db = Database(self._db_path)
        try:
            db.audit("pipeline.start")
            diagnostics.record("pipeline.start")
            # 예전 실행이 문서 하나를 반쯤 쓰다 죽었으면 여기서 되돌린다.
            db.repair_interrupted()
            self._scan(db)
            if not self._stop:
                self._hash(db)
            if not self._stop:
                self._parse(db)
            if not self._stop:
                self._date(db)
            if not self._stop:
                self._embed(db)
            if not self._stop:
                self._discover(db)
            if not self._stop:
                self._cycles(db)
            if not self._stop:
                self._steps(db)
            if not self._stop:
                self._restore(db)
            if not self._stop:
                self._chunks(db)
            db.audit("pipeline.stop", result="cancelled" if self._stop else "completed")
            diagnostics.record("pipeline.stop")
        except Exception as exc:  # 워커가 조용히 죽으면 사용자는 영문을 모른다
            diagnostics.record("pipeline.error", exc)
            import traceback

            db.audit("pipeline.error", detail=type(exc).__name__, result="failed")
            self.failed.emit(
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
            )
        finally:
            db.close()
            self.finished.emit()

    # ── 1단계: 찾기 ─────────────────────────────────────────────────
    def _scan(self, db: Database) -> None:
        sources = db.work_sources()
        report = StageReport(STAGE_SCAN)
        total = scanner.ScanStats()
        own = _own_paths(db)

        for source in sources:
            if self._stop:
                break
            stats = scanner.scan_source(
                db,
                source["id"],
                source["path"],
                source["excludes"],
                on_progress=lambda n, name: self.progress.emit(STAGE_SCAN, n, 0, name),
                should_stop=self._stopped,
                skip=own,
            )
            total.merge(stats)

        report.done = total.found
        report.total = total.found
        report.errors = total.errors[:20]
        report.note = (
            f"문서 {total.documents:,}건 · 새 파일 {total.new:,} · "
            f"변경 {total.changed:,} · 원본 없음 {total.missing:,}"
        )
        self.stage_done.emit(report)

    # ── 2단계: 해시 (완전 중복 판정) ────────────────────────────────
    def _hash(self, db: Database) -> None:
        rows = db.con.execute(
            "SELECT id, path FROM documents "
            "WHERE hash IS NULL AND missing_since IS NULL AND parse_status != 'skipped'"
        ).fetchall()
        report = StageReport(STAGE_HASH, total=len(rows))

        for index, row in enumerate(rows, start=1):
            if self._stop:
                break
            digest, error = sha256(row["path"])
            if digest:
                db.update_document(row["id"], hash=digest)
            elif error:
                report.errors.append(f"{Path(row['path']).name}: {error}")
            report.done = index
            if index % 20 == 0 or index == len(rows):
                self.progress.emit(STAGE_HASH, index, len(rows), Path(row["path"]).name)

        extra = db.counts()["duplicate_extra"]
        report.note = f"완전 중복 {extra:,}건" if extra else "중복 없음"
        self.stage_done.emit(report)

    # ── 3단계: 내용 읽기 ────────────────────────────────────────────
    def _parse(self, db: Database) -> None:
        rows = db.con.execute(
            """
            SELECT d.id, d.path, d.filename, d.hash
            FROM documents d
            JOIN sources s ON s.id = d.source_id AND s.kind != 'upload'
            LEFT JOIN jobs j ON j.doc_id = d.id AND j.kind = 'parse'
            WHERE d.missing_since IS NULL
              AND (d.parse_status = 'pending'
                   OR (d.parse_status IN ('ok','partial') AND d.parser_version != ?))
              AND COALESCE(j.attempts, 0) < ?
            ORDER BY d.id
            """,
            (PARSER_VERSION, MAX_ATTEMPTS),
        ).fetchall()
        report = StageReport(STAGE_PARSE, total=len(rows))
        failures = 0
        locked = 0

        for index, row in enumerate(rows, start=1):
            if self._stop:
                break
            # 해시 없는 문서를 '읽음'으로 올리지 않는다. 해시 단계가 이 파일을
            # 못 읽었거나 건너뛰었으면 여기서 한 번 더 뜬다. 끝내 못 뜨면 읽지
            # 않고 남겨 둔다 — 중복 판정이 빠진 문서가 준비 완료로 보이면 같은
            # 내용이 근거 목록에 여러 번 나온다.
            digest = row["hash"]
            if digest is None:
                digest, hash_error = sha256(row["path"])
                if digest is None:
                    locked += 1
                    report.errors.append(
                        f"{row['filename']}: 중복 확인을 못 해 건너뜀 ({hash_error})")
                    report.done = index
                    continue

            result = _parse_with_retry(row["path"])

            # 문서 하나의 기록은 전부 쓰이거나 전혀 안 쓰인다. 예전에는 문장마다
            # 따로 확정돼서, '읽음' 표시와 본문 저장 사이에 프로세스가 죽으면
            # 본문도 검색 색인도 없는 문서가 영영 '읽음'으로 남았다.
            with db.transaction():
                if row["hash"] is None:
                    db.update_document(row["id"], hash=digest)

                if result.ok:
                    # 파서 판본 변경으로 재처리한 문서도 옛 질문 조각을 남기면 안 된다.
                    db.invalidate_document_analysis(row["id"])

                db.update_document(
                    row["id"],
                    parse_status=result.status,
                    parse_error=result.error,
                    parse_note=result.note,
                    parser=result.parser,
                    parser_version=result.parser_version,
                    char_count=result.char_count,
                    author=result.meta.author,
                    doc_title=result.meta.title,
                    meta_created=result.meta.created,
                    meta_modified=result.meta.modified,
                )

                if result.ok:
                    db.replace_sections(
                        row["id"],
                        [(s.kind, s.ordinal, s.locator, s.text) for s in result.sections],
                    )
                    db.index_document(
                        row["id"], row["filename"], row["path"],
                        result.meta.author or "", result.text,
                    )
                    _clear_job(db, row["id"], "parse")
                elif result.status == LOCKED:
                    # 잠긴 파일은 실패로 굳히지 않는다. pending으로 되돌려
                    # 다음 실행에서 다시 읽고, 재시도 횟수도 세지 않는다 —
                    # 사용자가 문서를 닫기만 하면 정상 처리되어야 한다.
                    db.update_document(row["id"], parse_status="pending")
                    locked += 1
                    report.errors.append(f"{row['filename']}: 다른 프로그램이 열고 있어 건너뜀")
                else:
                    failures += 1
                    _record_failure(db, row["id"], "parse", result.error or result.status)
                    report.errors.append(f"{row['filename']}: {result.error or result.status}")

            report.done = index
            if index % 10 == 0 or index == len(rows):
                self.progress.emit(STAGE_PARSE, index, len(rows), row["filename"])

        counts = db.counts()
        report.note = f"읽음 {counts['parsed']:,}건"
        if failures:
            report.note += f" · 읽지 못함 {failures:,}건"
        if locked:
            report.note += f" · 열려 있어 건너뜀 {locked:,}건"
        report.errors = report.errors[:20]
        self.stage_done.emit(report)


    # ── 4단계: 시점 확인 ────────────────────────────────────────────
    def _date(self, db: Database) -> None:
        """When과 How 전체가 이 단계에 걸려 있다.

        후보를 다섯 갈래로 모아 우선순위대로 하나를 채택하되, 채택하지 않은
        후보도 남긴다. 화면에서 "무엇을 근거로 2024년이라 했는지" 보여줘야
        사용자가 검증하고 고칠 수 있다.
        """
        # date_decided_by='user' 제외가 핵심이다. 사용자가 "날짜 모름"으로
        # 확정하면 eff_date가 비는데, 그 빈칸만 보고 다시 추정하면 사용자의
        # 판단을 조용히 지우게 된다 (NFR-SAF-004).
        rows = db.con.execute(
            "SELECT id, filename, path, fs_mtime, meta_created, meta_modified "
            "FROM documents "
            "WHERE eff_date IS NULL AND missing_since IS NULL "
            "  AND parse_status != 'skipped' AND date_decided_by != 'user'"
        ).fetchall()
        report = StageReport(STAGE_DATE, total=len(rows))
        undated = 0

        for index, row in enumerate(rows, start=1):
            if self._stop:
                break
            sections = db.con.execute(
                "SELECT locator, text FROM document_sections "
                "WHERE doc_id = ? ORDER BY ordinal LIMIT ?",
                (row["id"], DATE_SECTIONS),
            ).fetchall()

            resolution = dating.collect(
                sections=[(s["locator"], s["text"]) for s in sections],
                filename=row["filename"],
                path=row["path"],
                meta_created=row["meta_created"],
                meta_modified=row["meta_modified"],
                fs_mtime=row["fs_mtime"],
            )

            with db.transaction():
                db.replace_dates(row["id"], [c.as_row() for c in resolution.candidates])
                db.update_document(
                    row["id"],
                    eff_date=resolution.value,
                    eff_date_kind=resolution.kind,
                    eff_precision=resolution.precision,
                    eff_year=resolution.year,
                    eff_month=resolution.month,
                )
            if resolution.value is None:
                undated += 1

            report.done = index
            if index % 20 == 0 or index == len(rows):
                self.progress.emit(STAGE_DATE, index, len(rows), row["filename"])

        breakdown = db.con.execute(
            "SELECT eff_date_kind AS kind, COUNT(*) AS n FROM documents "
            "WHERE eff_date IS NOT NULL GROUP BY eff_date_kind"
        ).fetchall()
        parts = [f"{_KIND_LABEL.get(r['kind'], r['kind'])} {r['n']:,}" for r in breakdown]
        report.note = " · ".join(parts) if parts else "시점을 찾지 못함"
        if undated:
            report.note += f" · 단서 없음 {undated:,}"
        self.stage_done.emit(report)


    # ── 5단계: 의미 색인 ────────────────────────────────────────────
    def _embed(self, db: Database) -> None:
        """전 문서를 벡터로 만든다. 업무 발견(군집화)의 재료다.

        생성 모델은 문서 1만 건에 27시간이 걸리지만 임베딩은 11분이면 끝난다.
        그래서 전수 처리는 여기서 하고, 생성 모델은 묶음 대표에만 쓴다.

        Ollama가 없으면 조용히 건너뛴다 — AI는 단일 장애점이 아니다.
        """
        client = self._client = project_client(db, OllamaClient)
        health = client.health()
        if not health.embedding_ready:
            self.stage_done.emit(
                StageReport(STAGE_EMBED, note=f"건너뜀 — {health.message}")
            )
            return

        rows = db.con.execute(
            """
            SELECT d.id, d.filename, di.body
            FROM documents d
            JOIN sources s ON s.id = d.source_id AND s.kind != 'upload'
            JOIN document_index di ON di.doc_id = d.id
            LEFT JOIN doc_embeddings e ON e.doc_id = d.id AND e.model = ?
            WHERE d.missing_since IS NULL
              AND d.parse_status IN ('ok', 'partial')
              AND e.doc_id IS NULL
            ORDER BY d.id
            """,
            (client.embed_model,),
        ).fetchall()
        report = StageReport(STAGE_EMBED, total=len(rows))

        for start in range(0, len(rows), EMBED_BATCH):
            if self._stop:
                break
            batch = rows[start : start + EMBED_BATCH]
            texts = [_embed_text(r["filename"], r["body"]) for r in batch]
            vectors, error = client.embed(texts)
            if error:
                report.errors.append(error)
                break   # 연결이 끊긴 상태에서 계속 두드릴 이유가 없다

            for row, vector, text in zip(batch, vectors, texts):
                db.save_doc_embedding(
                    row["id"], client.embed_model, len(vector), pack(vector), len(text)
                )
            report.done = min(start + len(batch), len(rows))
            self.progress.emit(STAGE_EMBED, report.done, len(rows), batch[-1]["filename"])

        total = db.counts()["embedded"]
        report.note = f"색인 {total:,}건"
        if report.errors:
            report.note += " · 중단됨"
        self.stage_done.emit(report)


    # ── 6단계: 업무 파악 (What) ─────────────────────────────────────
    def _discover(self, db: Database) -> None:
        """벡터를 묶어 업무 후보를 만들고 묶음마다 한 번씩 이름을 묻는다.

        문서마다 모델에게 분류를 물으면 1만 건에 7시간이 걸리지만, 묶은 뒤
        묶음당 한 번만 부르면 수십 번으로 끝난다. 이름이 흩어지는 것도 막는다.
        """
        if db.counts()["embedded"] == 0:
            self.stage_done.emit(
                StageReport(STAGE_DISCOVER, note="건너뜀 — 의미 색인이 없습니다")
            )
            return

        client = self._client = project_client(db, OllamaClient)
        report = StageReport(STAGE_DISCOVER)

        def progress(done: int, total: int, label: str) -> None:
            report.done, report.total = done, total
            self.progress.emit(STAGE_DISCOVER, done, total, label)

        result = discover(db, client, on_progress=progress)
        report.total = report.total or result.clusters
        report.done = report.total
        report.note = result.summary()
        report.errors = result.errors[:20]
        self.stage_done.emit(report)


    # ── 7단계: 일정 파악 (When) ─────────────────────────────────────
    def _cycles(self, db: Database) -> None:
        """업무마다 연도×월 격자를 만들어 반복 주기를 찾는다.

        AI를 쓰지 않는다 — 시점 데이터로 하는 순수 계산이라 Ollama가 꺼져
        있어도 이 단계는 항상 돈다(제품 원칙 2).
        """
        tasks = db.tasks()
        report = StageReport(STAGE_CYCLES, total=len(tasks))
        found = 0

        for index, task in enumerate(tasks, start=1):
            if self._stop:
                break
            rows = db.task_grid_documents(task["id"])
            docs = [
                timeline.DatedDoc(
                    doc_id=row["id"],
                    year=row["year"],
                    month=row["month"],
                    day=int(row["eff_date"][8:10]) if row["eff_precision"] == "day" else None,
                    trustworthy=row["eff_date_kind"] != "fs",
                )
                for row in rows
            ]
            grid = timeline.build_grid(docs)
            guess = timeline.detect_cycle(grid)

            cycles = []
            if guess:
                found += 1
                # 일(day) 힌트는 '매월 5~10일'처럼 매월 반복에만 붙인다. 연간
                # 반복에 붙이면 "매년 9~11월 12일"처럼 어느 달의 12일인지
                # 모호해져 오히려 혼란을 준다.
                hint = timeline.day_hint(docs, None) if guess.kind == timeline.MONTHLY else None
                cycles.append({
                    "kind": guess.kind,
                    "months": ",".join(str(m) for m in guess.months),
                    "day_hint": hint,
                    "years_observed": guess.years_observed,
                    "confidence": guess.confidence,
                    "evidence": ",".join(str(i) for i in guess.evidence_doc_ids),
                })
            with db.transaction():
                db.replace_task_cycles(task["id"], cycles)

            if index % 5 == 0 or index == len(tasks):
                self.progress.emit(STAGE_CYCLES, index, len(tasks), task["name"])

        report.done = report.total
        report.note = f"반복 업무 {found}개 발견" if tasks else "업무가 없습니다"
        # cycles_found==0(반복을 하나도 못 찾음)과 '아직 안 살펴봄'을 구분해야
        # 매 실행마다 파이프라인이 불필요하게 다시 도는 것을 막을 수 있다.
        # 중간에 멈췄으면 '살펴봤다'고 적지 않는다 — 적으면 남은 업무를 다음
        # 실행이 이어서 보지 않는다.
        if not self._stop:
            db.set_meta("cycles_checked", "1")
        self.stage_done.emit(report)


    # ── 8단계: 처리 순서 파악 (How) ─────────────────────────────────
    def _steps(self, db: Database) -> None:
        """업무·연도마다 문서를 시간순으로 늘어놓아 처리 순서를 재현한다.

        When과 같은 격자를 가로로 읽는 것이라 AI를 쓰지 않는다. 라벨링도
        규칙 기반이라(core/labeling.py) Ollama가 꺼져 있어도 이 단계는
        항상 돈다.
        """
        tasks = db.tasks()
        total_years = sum(len(db.task_years(t["id"])) for t in tasks)
        report = StageReport(STAGE_STEPS, total=total_years)
        done = 0
        steps_made = 0

        for task in tasks:
            if self._stop:
                break
            for year in db.task_years(task["id"]):
                if self._stop:
                    break
                rows = db.task_year_documents(task["id"], year)
                candidates = [
                    timeline.StepCandidate(
                        doc_id=row["id"],
                        month=row["month"],
                        day=int(row["eff_date"][8:10]) if row["eff_precision"] == "day" else None,
                        label=label_document(row["filename"]),
                    )
                    for row in rows
                ]
                steps = timeline.reconstruct_steps(candidates, year)
                with db.transaction():
                    db.replace_task_steps(task["id"], year, [
                        {
                            "ordinal": s.ordinal, "label": s.label, "month": s.month,
                            "day_hint": s.day_hint, "doc_id": s.doc_id, "gap_note": s.gap_note,
                        }
                        for s in steps
                    ])
                steps_made += len(steps)
                done += 1
                if done % 5 == 0 or done == total_years:
                    self.progress.emit(STAGE_STEPS, done, total_years, task["name"])

        report.done = report.total
        report.note = f"{steps_made:,}단계 재구성" if steps_made else "재구성할 자료가 없습니다"
        if not self._stop:
            db.set_meta("steps_checked", "1")
        self.stage_done.emit(report)


    # ── 9단계: 업무 복원 (항목·근거) ────────────────────────────────
    def _restore(self, db: Database) -> None:
        """업무마다 문서에 명시된 기한·금액·제출처·절차 등을 근거와 함께 올린다.

        항목·절차·공백은 규칙으로만 읽으므로 Ollama가 꺼져 있어도 돈다. 전임자의
        입력을 기다리지 않는다 — 후임자가 처음 여는 화면에 이미 채워져 있어야 한다.
        모델이 준비돼 있으면 문장으로만 적힌 내용을 '추정'으로 덧붙인다.
        """
        report = StageReport(STAGE_RESTORE)
        # 가져온 복원본의 근거나, 자료 폴더를 옮겨 끊긴 근거를 지금 있는 문서에
        # 다시 잇는다. 문서를 훑고 해시를 뜬 뒤라야 찾을 수 있어 여기서 한다.
        transfer.relink_evidence(db)
        # 이름표 없이 문장으로만 적힌 내용은 생성 모델이 고른다. 의미 색인이 없는
        # 프로젝트(AI 없이 쓰는 경우)와 모델이 준비되지 않은 경우에는 부르지 않는다.
        client = None
        if db.counts()["embedded"]:
            candidate = self._client = project_client(db, OllamaClient)
            if candidate.health().generation_ready:
                client = candidate
        totals = reconstruction.restore(db, should_stop=self._stopped, client=client)
        report.done = report.total = totals["tasks"]
        if not totals["tasks"]:
            report.note = "업무가 없습니다"
        elif not totals["items"]:
            report.note = "문서에 명시된 항목을 찾지 못했습니다"
            if totals["steps"]:
                report.note += f" · 과거 수행 사례 {totals['steps']:,}단계"
        else:
            report.note = f"항목 {totals['items']:,}개"
            if totals["stated_steps"]:
                report.note += f" · 문서에 적힌 절차 {totals['stated_steps']:,}단계"
            if totals["conflicts"]:
                report.note += f" · 자료가 엇갈림 {totals['conflicts']:,}"
            if totals["recheck"]:
                report.note += f" · 재확인 필요 {totals['recheck']:,}"
        if totals["statements"]:
            report.note += f" · 문장에서 찾은 내용 {totals['statements']:,}"
        if totals["gaps"]:
            report.note += f" · 확인할 공백 {totals['gaps']:,}"
        if not self._stop:
            db.set_meta("items_checked", "1")
            # 복원이 끝난 판을 굳혀 둔다. 전임자의 확인을 기다리지 않는다 —
            # 내용이 지난번과 같으면 새로 만들지 않는다.
            saved = handover_release.autosave(db)
            if saved and saved[1]:
                report.note += " · 복원본 저장"
        self.stage_done.emit(report)


    # ── 10단계: 질문 준비 (RAG 재료) ────────────────────────────────
    def _chunks(self, db: Database) -> None:
        """문서를 조각으로 나누고 임베딩한다 — 질문 화면의 재료다.

        파서가 이미 나눠 둔 문단·표·페이지·시트·슬라이드 경계를 그대로
        쓴다(core/chunking.py). 조각 나누기는 모델이 필요 없는 일이라 항상
        한다 — 조각과 조각 글자 검색(chunk_fts)은 Ollama 없이도 생긴다.
        임베딩만 Ollama를 기다린다. AI는 단일 장애점이 아니다.
        """
        client = self._client = project_client(db, OllamaClient)
        report = StageReport(STAGE_CHUNKS)
        made = 0
        embedded = 0

        # 조각이 이미 있는 문서는 다시 만들지 않는다. 다시 만들면 chunk_id가
        # 바뀌어 멀쩡히 있던 임베딩까지 CASCADE로 함께 지워진다.
        bare = db.documents_without_chunks()
        for index, row in enumerate(bare, start=1):
            if self._stop:
                break
            sections = db.con.execute(
                "SELECT kind, locator, text FROM document_sections "
                "WHERE doc_id = ? ORDER BY ordinal",
                (row["id"],),
            ).fetchall()
            pieces = build_chunks(
                [(s["kind"], s["locator"], s["text"]) for s in sections]
            )
            if pieces:
                # 조각을 지우고 다시 넣는 사이에 죽으면 조각이 반만 남는다. 그 문서는
                # '조각이 있는 문서'로 분류돼 나머지 조각을 다시 만들지 않는다.
                with db.transaction():
                    made += len(db.replace_document_chunks(row["id"], pieces))
            if index % 20 == 0 or index == len(bare):
                self.progress.emit(STAGE_CHUNKS, index, len(bare), row["filename"])

        health = client.health()
        if not health.embedding_ready:
            # chunks_checked는 세우지 않는다 — 연결되면 임베딩을 이어서 해야 한다.
            report.done = report.total = len(bare)
            report.note = f"임베딩 보류 — {health.message}"
            if made:
                report.note = f"{made:,}조각 나눔 · " + report.note
            self.stage_done.emit(report)
            return

        # '조각은 있는데 임베딩이 빠진' 문서를 집어낸다. 임베딩이 배치 중간에
        # 실패하거나 모델 없이 조각만 만들어 둔 문서를 다음 실행에서 건너뛰면
        # 질문 화면에서 영영 사라진다.
        rows = db.documents_needing_chunks(client.embed_model)
        report.total = len(rows)

        for index, row in enumerate(rows, start=1):
            if self._stop:
                break

            pending = db.chunks_needing_embedding(row["id"], client.embed_model)
            for start in range(0, len(pending), CHUNK_EMBED_BATCH):
                if self._stop:
                    break
                batch = pending[start : start + CHUNK_EMBED_BATCH]
                vectors, error = client.embed([text for _, text in batch])
                if error:
                    report.errors.append(f"{row['filename']}: {error}")
                    break
                with db.transaction():
                    for (chunk_id, _text), vector in zip(batch, vectors):
                        db.save_chunk_embedding(
                            chunk_id, client.embed_model, len(vector), pack(vector))
                        embedded += 1

            report.done = index
            if index % 5 == 0 or index == len(rows):
                self.progress.emit(STAGE_CHUNKS, index, len(rows), row["filename"])

        left = db.unembedded_chunk_count(client.embed_model)
        if made or embedded:
            report.note = f"{embedded:,}조각 준비"
        else:
            report.note = "준비할 자료가 없습니다"
        if left:
            # 남은 것을 숨기지 않는다. 질문 화면이 못 보는 자료가 있다는 뜻이다.
            report.note += f" · {left:,}조각 남음"
        if report.errors:
            report.note += " · 일부 실패"
        if not self._stop:
            db.set_meta("chunks_checked", "1")
        self.stage_done.emit(report)


def _own_paths(db: Database) -> list[Path]:
    """이 프로그램이 프로젝트 폴더에 만든 것들 — 자료로 훑으면 안 된다."""
    folder = db.path.parent
    # outputs는 내보낸 보고서·산출물의 기본 저장 폴더다. 이 프로그램이 쓴 글이
    # 전임자 문서로 되돌아오면 자기가 쓴 문장을 근거로 삼게 된다.
    own = [folder / "logs", folder / OUTPUT_DIR, *folder.glob(db.path.name + "*")]
    own += [Path(s["path"]) for s in db.sources() if s["kind"] == "upload"]
    return own


def _embed_text(filename: str, body: str) -> str:
    """파일명을 앞에 붙인다. 공직 자료는 파일명에 업무 단서가 많이 들어 있다."""
    head = " ".join((body or "").split())[:EMBED_CHARS]
    return f"{filename}\n{head}".strip()


_KIND_LABEL = {
    "body": "본문",
    "filename": "파일명",
    "meta": "문서속성",
    "folder": "폴더",
    "fs": "파일날짜",
}


class PipelineRunner(QObject):
    """UI 쪽에서 파이프라인을 켜고 끄는 손잡이.

    QThread를 직접 상속하지 않고 moveToThread를 쓴다. 그래야 워커 객체의
    슬롯이 워커 스레드에서 돌고, DB 연결이 스레드를 넘나들지 않는다.
    """

    progress = Signal(str, int, int, str)
    stage_done = Signal(object)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, db_path: Path | str, parent: QObject | None = None):
        super().__init__(parent)
        self._db_path = db_path
        self._thread: QThread | None = None
        self._pipeline: Pipeline | None = None

        # 창의 closeEvent를 거치지 않고 끝나는 종료 경로가 있다. 실행 중인
        # QThread가 그대로 파괴되면 프로세스가 죽으므로 종료 신호에도 건다.
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread()
        self._pipeline = Pipeline(self._db_path)
        self._pipeline.moveToThread(self._thread)

        self._pipeline.progress.connect(self.progress)
        self._pipeline.stage_done.connect(self.stage_done)
        self._pipeline.failed.connect(self.failed)
        self._pipeline.finished.connect(self._cleanup)
        self._thread.started.connect(self._pipeline.run)
        self._thread.start()

    def stop(self, wait_ms: int = 5000) -> None:
        """일시정지 요청 후 안전 지점에서 멈춘다."""
        if self._pipeline is not None:
            self._pipeline.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(wait_ms)

    def _cleanup(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread.deleteLater()
        if self._pipeline is not None:
            self._pipeline.deleteLater()
        self._thread = None
        self._pipeline = None
        self.finished.emit()


def _parse_with_retry(path: str, attempts: int = LOCK_RETRIES) -> "ParseResult":
    """잠긴 파일은 짧게 몇 번 다시 시도한다.

    파일 잠김은 대개 순간적이다 — 다른 프로세스가 막 닫는 중이거나,
    강제 종료된 프로세스의 핸들을 OS가 아직 회수하지 않은 경우다. 실제로
    강제종료 재개 시험에서 이 상황이 재현됐다. 몇 번 기다렸다 다시 열면
    대부분 풀리고, 끝내 안 풀리면 LOCKED로 남겨 다음 실행에 넘긴다.
    """
    result = parse(path)
    for _ in range(attempts - 1):
        if result.status != LOCKED:
            return result
        time.sleep(LOCK_RETRY_DELAY)
        result = parse(path)
    return result


def _record_failure(db: Database, doc_id: int, kind: str, error: str) -> None:
    db.con.execute(
        "INSERT INTO jobs(kind, doc_id, state, attempts, error, tool_version) "
        "VALUES (?, ?, 'failed_retryable', 1, ?, ?) "
        "ON CONFLICT(kind, doc_id) DO UPDATE SET "
        "  attempts = jobs.attempts + 1, error = excluded.error, "
        "  state = CASE WHEN jobs.attempts + 1 >= ? THEN 'failed_final' ELSE 'failed_retryable' END, "
        "  updated_at = datetime('now')",
        (kind, doc_id, error[:500], PARSER_VERSION, MAX_ATTEMPTS),
    )


def _clear_job(db: Database, doc_id: int, kind: str) -> None:
    db.con.execute("DELETE FROM jobs WHERE kind = ? AND doc_id = ?", (kind, doc_id))
