"""스키마 마이그레이션.

프로젝트 하나가 DB 파일 하나다. 그 안에 몇 시간~며칠짜리 분석 결과가 들어
있고, 사용자가 손으로 고친 교정값이 쌓인다. 스키마를 올릴 때 이것을 잃으면
제품 신뢰가 한 번에 무너진다.

원칙
  1. 올리기 전에 항상 백업한다. (project.db.bak-v1)
  2. 각 단계는 트랜잭션이다. 실패하면 통째로 되돌리고 옛 버전을 유지한다.
  3. 앱이 DB보다 낮은 버전이면 열지 않는다 — 내려쓰기는 지원하지 않는다.
  4. 마이그레이션 결과는 새로 설치한 스키마와 같아야 한다 (tests에서 강제).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

# 이 앱이 쓰는 스키마 버전. schema.sql과 함께 올린다.
SCHEMA_VERSION = "13"


def column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in con.execute(f"PRAGMA table_info({table})"))


def add_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """이미 있으면 조용히 넘긴다. CREATE TABLE IF NOT EXISTS는 열을 붙이지 않는다."""
    if not column_exists(con, table, column):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# ── v1 → v2 ─────────────────────────────────────────────────────────
# 교정 UX(계획서 §11)와 인수인계 진행도(§18), 업무기억 건강도(§19)의 자리를
# 만든다. 기존 표는 지우지 않고 열만 늘린다.

def _to_v2(con: sqlite3.Connection) -> None:
    # 업무 — 확인 상태, '업무 아님', 합치기 흔적
    add_column(con, "tasks", "review_state",
               "review_state TEXT NOT NULL DEFAULT 'inferred'")
    add_column(con, "tasks", "reviewed_at", "reviewed_at TEXT")
    add_column(con, "tasks", "not_a_task", "not_a_task INTEGER NOT NULL DEFAULT 0")
    add_column(con, "tasks", "merged_into",
               "merged_into INTEGER REFERENCES tasks(id)")

    # 대표 문서 지정
    add_column(con, "task_docs", "is_primary",
               "is_primary INTEGER NOT NULL DEFAULT 0")

    # 시점 교정 — 후보(document_dates)는 그대로 두고 '누가 골랐는가'만 기록
    add_column(con, "documents", "date_decided_by",
               "date_decided_by TEXT NOT NULL DEFAULT 'ai'")

    for statement in _V2_TABLES:
        con.execute(statement)

    # 데이터 이관: 이미 사람이 확인·수정한 업무는 confirmed로 올린다.
    # 이걸 빠뜨리면 사용자가 어제 확인한 업무가 오늘 다시 '미확인'으로 보인다.
    con.execute(
        "UPDATE tasks SET review_state = 'confirmed' "
        "WHERE status IN ('approved', 'edited') OR origin = 'user'"
    )
    con.execute(
        "UPDATE tasks SET review_state = 'weak' "
        "WHERE review_state = 'inferred' AND confidence = 'low'"
    )


# 신규 설치(schema.sql)와 같은 결과를 내야 한다. 어긋나면
# tests/test_migrations.py가 잡는다.
#
# executescript()를 쓰지 않는 이유: sqlite3 모듈은 executescript 직전에
# 열려 있던 트랜잭션을 암묵적으로 COMMIT한다. 그러면 아래 upgrade()의
# BEGIN이 풀려 롤백 보장이 사라진다. 문장 단위로 실행한다.
_V2_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS handover_checks (
        id         INTEGER PRIMARY KEY,
        kind       TEXT NOT NULL,
        target_id  INTEGER,
        state      TEXT NOT NULL DEFAULT 'pending',
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (kind, target_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_health (
        task_id    INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        score      INTEGER NOT NULL DEFAULT 0,
        findings   TEXT,
        checked_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
)


# ── v2 → v3 ─────────────────────────────────────────────────────────
# 조각 단위 정확 일치 검색(질문화면 RAG 개선 R3). document_fts는 문서
# 단위라 어느 조각이 일치했는지 알 수 없다.

def _to_v3(con: sqlite3.Connection) -> None:
    for statement in _V3_STATEMENTS:
        con.execute(statement)
    # 외부content FTS5 표는 만든 시점 이후의 변경만 트리거로 따라간다.
    # 이미 있던 조각은 트리거를 못 봤으므로 명시적으로 다시 채워야 한다.
    con.execute("INSERT INTO chunk_fts(chunk_fts) VALUES ('rebuild')")


_V3_STATEMENTS = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
        text, content='chunks', content_rowid='id', tokenize='trigram'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
        INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
)


# ── v3 → v4 ─────────────────────────────────────────────────────────
# 온나라 공식 출처(구축계획서 §5.1). 새 표만 만든다 — 기존 문서·교정·업무
# 연결은 건드리지 않는다. 열 정의는 schema.sql과 같아야 한다.

def _to_v4(con: sqlite3.Connection) -> None:
    for statement in _V4_STATEMENTS:
        con.execute(statement)


_V4_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS collection_runs (
        id                 INTEGER PRIMARY KEY,
        collection_key     TEXT NOT NULL UNIQUE,
        adapter            TEXT NOT NULL,
        institution        TEXT NOT NULL,
        system_id          TEXT NOT NULL,
        scope              TEXT NOT NULL,
        simulation         INTEGER NOT NULL DEFAULT 0,
        live_verified      INTEGER NOT NULL DEFAULT 0,
        status             TEXT,
        completeness       TEXT,
        expected_documents INTEGER,
        documents          INTEGER NOT NULL DEFAULT 0,
        assets             INTEGER NOT NULL DEFAULT 0,
        downloaded         INTEGER NOT NULL DEFAULT 0,
        failed             INTEGER NOT NULL DEFAULT 0,
        started_at         TEXT,
        collected_at       TEXT,
        registered_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_records (
        id              INTEGER PRIMARY KEY,
        run_id          INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
        institution     TEXT NOT NULL,
        system_id       TEXT NOT NULL,
        document_id     TEXT NOT NULL,
        title           TEXT NOT NULL DEFAULT '',
        document_number TEXT NOT NULL DEFAULT '',
        author_id       TEXT NOT NULL DEFAULT '',
        author_name     TEXT NOT NULL DEFAULT '',
        department      TEXT NOT NULL DEFAULT '',
        author_role     TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'unknown',
        authored_at     TEXT,
        registered_at   TEXT,
        approved_at     TEXT,
        first_collected TEXT NOT NULL DEFAULT (datetime('now')),
        last_collected  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (institution, system_id, document_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_assets (
        id            INTEGER PRIMARY KEY,
        record_id     INTEGER NOT NULL REFERENCES source_records(id) ON DELETE CASCADE,
        asset_id      TEXT NOT NULL,
        role          TEXT NOT NULL,
        filename      TEXT NOT NULL DEFAULT '',
        media_type    TEXT NOT NULL DEFAULT '',
        sha256        TEXT,
        size          INTEGER,
        status        TEXT NOT NULL DEFAULT 'pending',
        error         TEXT,
        downloaded_at TEXT,
        doc_id        INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        UNIQUE (record_id, asset_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_assets_doc ON source_assets(doc_id)",
    "CREATE INDEX IF NOT EXISTS idx_source_assets_hash ON source_assets(sha256)",
)


# ── v4 → v5 ─────────────────────────────────────────────────────────
# 업무 복원·수행(구축계획서 §5.1). 새 표만 만든다 — 기존 업무·교정·인계
# 확인 표시는 건드리지 않는다. 열 정의는 schema.sql과 같아야 한다.

def _to_v5(con: sqlite3.Connection) -> None:
    for statement in _V5_STATEMENTS:
        con.execute(statement)


_V5_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS work_contexts (
        id           INTEGER PRIMARY KEY,
        mode         TEXT NOT NULL DEFAULT 'materials',
        period_start TEXT,
        period_end   TEXT,
        basis_at     TEXT NOT NULL DEFAULT (datetime('now')),
        predecessor  TEXT,
        note         TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_items (
        id            INTEGER PRIMARY KEY,
        task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        context_id    INTEGER REFERENCES work_contexts(id) ON DELETE SET NULL,
        kind          TEXT NOT NULL,
        slot          TEXT NOT NULL DEFAULT '',
        content       TEXT NOT NULL DEFAULT '',
        auto_content  TEXT,
        origin        TEXT NOT NULL DEFAULT 'auto',
        basis_state   TEXT NOT NULL DEFAULT 'stated',
        applicability TEXT NOT NULL DEFAULT 'unknown',
        human_state   TEXT NOT NULL DEFAULT 'none',
        excluded      INTEGER NOT NULL DEFAULT 0,
        version       INTEGER NOT NULL DEFAULT 1,
        extractor     TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (task_id, kind, slot)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_steps (
        id             INTEGER PRIMARY KEY,
        task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        context_id     INTEGER REFERENCES work_contexts(id) ON DELETE SET NULL,
        slot           TEXT NOT NULL DEFAULT '',
        ordinal        INTEGER NOT NULL DEFAULT 0,
        label          TEXT NOT NULL,
        auto_label     TEXT,
        inputs         TEXT,
        forms          TEXT,
        output         TEXT,
        done_criteria  TEXT,
        procedure_kind TEXT NOT NULL DEFAULT 'precedent',
        after_step_id  INTEGER REFERENCES work_steps(id) ON DELETE SET NULL,
        task_step_id   INTEGER REFERENCES task_steps(id) ON DELETE SET NULL,
        origin         TEXT NOT NULL DEFAULT 'auto',
        basis_state    TEXT NOT NULL DEFAULT 'stated',
        applicability  TEXT NOT NULL DEFAULT 'unknown',
        human_state    TEXT NOT NULL DEFAULT 'none',
        excluded       INTEGER NOT NULL DEFAULT 0,
        version        INTEGER NOT NULL DEFAULT 1,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (task_id, slot)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_work_steps_task ON work_steps(task_id, ordinal)",
    """
    CREATE TABLE IF NOT EXISTS evidence (
        id           INTEGER PRIMARY KEY,
        owner_kind   TEXT NOT NULL,
        owner_id     INTEGER NOT NULL,
        doc_id       INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        doc_hash     TEXT,
        locator      TEXT NOT NULL DEFAULT '',
        quote        TEXT NOT NULL DEFAULT '',
        extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
        doc_name     TEXT,
        doc_relpath  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_evidence_owner ON evidence(owner_kind, owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_doc   ON evidence(doc_id)",
    """
    CREATE TABLE IF NOT EXISTS info_gaps (
        id          INTEGER PRIMARY KEY,
        task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        step_id     INTEGER REFERENCES work_steps(id) ON DELETE SET NULL,
        item_id     INTEGER REFERENCES task_items(id) ON DELETE SET NULL,
        kind        TEXT NOT NULL,
        origin      TEXT NOT NULL DEFAULT 'user',
        gap_key     TEXT NOT NULL DEFAULT '',
        field       TEXT NOT NULL DEFAULT '',
        detail      TEXT NOT NULL DEFAULT '',
        research    TEXT,
        next_action TEXT,
        state       TEXT NOT NULL DEFAULT 'open',
        resolution  TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        resolved_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_info_gaps_task ON info_gaps(task_id, state)",
    """
    CREATE TABLE IF NOT EXISTS step_runs (
        step_id       INTEGER PRIMARY KEY REFERENCES work_steps(id) ON DELETE CASCADE,
        state         TEXT NOT NULL DEFAULT 'not_started',
        started_at    TEXT,
        finished_at   TEXT,
        result_doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        result_path   TEXT,
        note          TEXT,
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id             INTEGER PRIMARY KEY,
        target_kind    TEXT NOT NULL,
        target_id      INTEGER NOT NULL,
        target_version INTEGER NOT NULL DEFAULT 1,
        action         TEXT NOT NULL,
        before_val     TEXT,
        after_val      TEXT,
        reviewer_name  TEXT NOT NULL DEFAULT '',
        reviewer_role  TEXT NOT NULL DEFAULT 'successor',
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reviews_target ON reviews(target_kind, target_id)",
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id           INTEGER PRIMARY KEY,
        kind         TEXT NOT NULL DEFAULT 'restoration',
        label        TEXT NOT NULL DEFAULT '',
        context_id   INTEGER REFERENCES work_contexts(id) ON DELETE SET NULL,
        basis_at     TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        summary      TEXT NOT NULL,
        payload      TEXT NOT NULL,
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snapshots_kind ON snapshots(kind, id)",
    """
    CREATE TABLE IF NOT EXISTS snapshot_receipts (
        id            INTEGER PRIMARY KEY,
        snapshot_id   INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
        content_hash  TEXT NOT NULL,
        receiver_name TEXT NOT NULL DEFAULT '',
        relink        TEXT NOT NULL DEFAULT '{}',
        received_at   TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS task_items_evidence_ad AFTER DELETE ON task_items BEGIN
        DELETE FROM evidence WHERE owner_kind = 'item' AND owner_id = old.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS work_steps_evidence_ad AFTER DELETE ON work_steps BEGIN
        DELETE FROM evidence WHERE owner_kind = 'step' AND owner_id = old.id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS info_gaps_evidence_ad AFTER DELETE ON info_gaps BEGIN
        DELETE FROM evidence WHERE owner_kind = 'gap' AND owner_id = old.id;
    END
    """,
)


# 버전 문자열 → 그 버전으로 올리는 함수.
def _to_v6(con: sqlite3.Connection) -> None:
    # 신규 표만 추가한다. 기존 문서·업무·검토·수행 기록은 변경하지 않는다.
    for statement in Path(__file__).with_name("notebook_schema.sql").read_text(encoding="utf-8").split(";"):
        if statement.strip():
            con.execute(statement)


# v7이 기존 노트북 표에 더하는 열. ALTER TABLE은 상수 기본값만 받으므로
# 시각 열은 비워 두고 쓰는 쪽(core/notebook.py)이 채운다. 새 프로젝트도 같은
# 목록을 지나므로(db/repo.py init) 승격 결과와 신규 설치가 같아진다.
NOTEBOOK_COLUMNS = (
    ("notebooks", "icon", "icon TEXT NOT NULL DEFAULT ''"),
    ("notebooks", "updated_at", "updated_at TEXT"),
    ("notebooks", "last_opened_at", "last_opened_at TEXT"),
    ("notebooks", "layout", "layout TEXT NOT NULL DEFAULT ''"),
    ("notebook_sources", "origin", "origin TEXT NOT NULL DEFAULT 'project'"),
    ("notebook_sources", "added_at", "added_at TEXT"),
    ("notebook_sources", "summary", "summary TEXT NOT NULL DEFAULT ''"),
    ("notebook_sources", "topics", "topics TEXT NOT NULL DEFAULT '[]'"),
    # 업무 정리본이 어느 업무의 것인가(core/notebook_tasks.py). 버전을 올리지 않고 붙는 열이다.
    ("notebook_sources", "task_id", "task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL"),
    ("notebook_messages", "thread_id", "thread_id INTEGER NOT NULL DEFAULT 1"),
    ("notebook_messages", "standalone_question", "standalone_question TEXT NOT NULL DEFAULT ''"),
    ("notebook_messages", "law_citations", "law_citations TEXT NOT NULL DEFAULT '[]'"),
    ("notebook_messages", "mode", "mode TEXT NOT NULL DEFAULT 'rag'"),
    ("notebook_messages", "rating", "rating TEXT"),
    ("notebook_notes", "kind", "kind TEXT NOT NULL DEFAULT 'answer'"),
)


def add_notebook_columns(con: sqlite3.Connection) -> None:
    for table, column, ddl in NOTEBOOK_COLUMNS:
        add_column(con, table, column, ddl)


def _to_v7(con: sqlite3.Connection) -> None:
    # 새 표(법령 캐시·서식·산출물)와 노트북 표의 새 열. 기존 행은 바꾸지 않는다.
    _to_v6(con)
    add_notebook_columns(con)


def _to_v8(con: sqlite3.Connection) -> None:
    # complete_statement handles trigger bodies without executescript's implicit COMMIT.
    statement = ""
    for line in Path(__file__).with_name("child_schema.sql").read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""


def _to_v9(con: sqlite3.Connection) -> None:
    statement = ""
    for line in Path(__file__).with_name("coverage_schema.sql").read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""


def _to_v10(con: sqlite3.Connection) -> None:
    # 수행 건·제안·실행·산출물. 새 표만 더하고 기존 행은 바꾸지 않는다.
    statement = ""
    for line in Path(__file__).with_name("action_schema.sql").read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""


def _to_v11(con: sqlite3.Connection) -> None:
    add_column(con, "documents", "extraction_coverage", "extraction_coverage TEXT NOT NULL DEFAULT '{}'")


def _to_v12(con: sqlite3.Connection) -> None:
    from .knowledge import install

    install(con)


def _to_v13(con: sqlite3.Connection) -> None:
    from .knowledge import install

    add_column(con, "kg_jobs", "lease_until", "lease_until REAL")
    add_column(con, "kg_jobs", "leased_revision", "leased_revision INTEGER")
    add_column(con, "kg_jobs", "error", "error TEXT NOT NULL DEFAULT ''")
    con.execute("DROP TRIGGER IF EXISTS kg_claim_domain")
    install(con)
    con.execute("UPDATE kg_jobs SET state='dirty',lease=NULL,lease_until=NULL,attempts=0,reason='schema13' "
                "WHERE partition_key IN (SELECT partition_key FROM kg_generations)")


STEPS: dict[str, Callable[[sqlite3.Connection], None]] = {
    "2": _to_v2,
    "3": _to_v3,
    "4": _to_v4,
    "5": _to_v5,
    "6": _to_v6,
    "7": _to_v7,
    "8": _to_v8,
    "9": _to_v9,
    "10": _to_v10,
    "11": _to_v11,
    "12": _to_v12,
    "13": _to_v13,
}


class MigrationError(RuntimeError):
    """마이그레이션 실패. 메시지는 사용자에게 그대로 보여도 되는 한글이다."""


def upgrade(con: sqlite3.Connection, path: Path, current: str) -> str:
    """current에서 SCHEMA_VERSION까지 순서대로 올린다. 최종 버전을 돌려준다."""
    if current == SCHEMA_VERSION:
        return current

    try:
        start = int(current)
        target = int(SCHEMA_VERSION)
    except ValueError as exc:
        raise MigrationError(f"알 수 없는 스키마 버전입니다: {current}") from exc

    if start > target:
        raise MigrationError(
            f"이 프로젝트는 더 새로운 버전(v{current})에서 만들어졌습니다. "
            f"현재 앱은 v{SCHEMA_VERSION}까지 지원합니다. 앱을 최신으로 올리세요."
        )

    backup(con, path, current)

    version = current
    for step in range(start + 1, target + 1):
        name = str(step)
        migrate = STEPS.get(name)
        if migrate is None:
            raise MigrationError(f"v{name}으로 올리는 방법이 없습니다.")
        con.execute("BEGIN")
        try:
            migrate(con)
            con.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (name,),
            )
            con.execute("COMMIT")
        except Exception as exc:
            con.execute("ROLLBACK")
            raise MigrationError(
                f"프로젝트를 v{name} 형식으로 올리지 못했습니다: {exc}\n"
                f"원래 상태로 되돌렸습니다. 백업: {backup_path(path, current).name}"
            ) from exc
        version = name

    return version


def backup_path(path: Path, version: str) -> Path:
    return path.with_name(f"{path.name}.bak-v{version}")


def backup(con: sqlite3.Connection, path: Path, version: str) -> Path:
    """SQLite 백업 API로 복사한다.

    파일을 그냥 copy하면 WAL 모드에서 아직 본체에 반영되지 않은 내용이
    빠진다. backup()은 열려 있는 연결에서도 일관된 사본을 만든다.
    """
    target = backup_path(path, version)
    with sqlite3.connect(target) as sink:
        con.backup(sink)
    return target
