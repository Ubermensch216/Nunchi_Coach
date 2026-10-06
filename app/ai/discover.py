"""업무 발견 — What의 본체.

"당신이 인수받은 업무는 7개로 추정됩니다"를 만드는 곳이다.

순서
  1. 문서 벡터를 묶는다 (규칙·수치 계산, 모델 없음)
  2. 묶음마다 한 번씩만 모델에게 이름을 묻는다
  3. 이름을 못 얻으면 파일명 공통 낱말로 임시 이름을 만든다
  4. 묶음마다 먼저 읽을 문서를 규칙으로 고른다

모델을 못 써도 1·3·4는 동작한다. 업무 목록이 아예 안 나오는 상황을 만들지 않는다.

발견 결과는 **제안**이다. 사용자가 승인해야 공식 업무가 된다(KNW-001).
사용자가 고친 업무와 직접 배정한 문서는 재발견이 덮어쓰지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..core import scoring
from ..core.clustering import DEFAULT_MIN_SIZE, Cluster, _tokenize, cluster, common_tokens
from ..db import Database
from ..search.vector import VectorStore
from .analyze import name_task
from .client import OllamaClient

MAX_TITLES_FOR_NAMING = 30


@dataclass(slots=True)
class Discovery:
    clusters: int = 0
    tasks: int = 0
    assigned: int = 0
    unassigned: int = 0
    named_by_model: int = 0
    named_by_filename: int = 0
    grouped_by_filename: int = 0     # 벡터로 묶이지 않아 파일명으로 묶은 제안
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"업무 {self.tasks}개", f"배정 {self.assigned:,}건"]
        if self.grouped_by_filename:
            parts.append(f"파일명으로 묶은 제안 {self.grouped_by_filename}개")
        if self.unassigned:
            parts.append(f"미분류 {self.unassigned:,}건")
        if self.named_by_filename:
            parts.append(f"파일명으로 이름 지음 {self.named_by_filename}개")
        return " · ".join(parts)


def discover(
    db: Database,
    client: OllamaClient | None = None,
    threshold: float | None = None,
    min_size: int | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> Discovery:
    result = Discovery()

    store = VectorStore.load(db.con, model=client.embed_model if client else None)
    if store.size == 0:
        result.errors.append("의미 색인이 없습니다. 먼저 분석을 끝내세요.")
        return result

    kwargs = {}
    if threshold is not None:
        kwargs["threshold"] = threshold
    if min_size is not None:
        kwargs["min_size"] = min_size
    grouping = cluster(store, **kwargs)

    result.clusters = len(grouping.clusters)
    result.unassigned = len(grouping.unassigned)

    _clear_proposals(db)
    naming_ready = bool(client and client.health().generation_ready)

    for index, group in enumerate(grouping.clusters, start=1):
        if client is not None and getattr(client, "cancelled", False):
            break
        names = _filenames(db, group.doc_ids)
        tokens = common_tokens(names)

        named = _name(client if naming_ready else None, names, tokens)
        if named.error:
            result.errors.append(named.error)
        if named.source == "model":
            result.named_by_model += 1
        else:
            result.named_by_filename += 1

        task_id = _upsert_task(db, named, group)
        assigned = _attach(db, task_id, group.doc_ids)
        result.assigned += assigned
        _store_reading(db, task_id, group.doc_ids)

        if on_progress:
            on_progress(index, len(grouping.clusters), named.label)

    if not (client is not None and getattr(client, "cancelled", False)):
        _group_leftovers(db, grouping.unassigned, min_size or DEFAULT_MIN_SIZE, result)

    result.tasks = db.con.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    db.audit("task.discover", detail=result.summary(), result="ok")
    return result


# ── 묶이지 않은 문서 ────────────────────────────────────────────────
# 업무 복원('지금 할 일')은 업무에 묶인 문서에서만 일어난다. 문서가 몇 건뿐이고
# 형식이 섞인 업무(계약 체결 보고·회의록·정산 보고)는 내용이 충분히 비슷하지
# 않아 벡터로는 묶이지 않고, 그러면 그 업무에는 카드가 아예 생기지 않는다
# (단계 4 검증에서 시나리오 세 묶음 중 둘이 그랬다).
#
# 군집 기준을 낮추지 않는다 — 이미 묶인 업무의 분류가 흐트러진다. 대신 남은
# 문서만 놓고 파일명이 같은 낱말로 시작하는 것끼리 '낮은 확신'의 제안으로
# 묶는다. 이미 있는 업무에는 밀어 넣지 않는다.

def _group_leftovers(db: Database, doc_ids: list[int], min_size: int, result: Discovery) -> None:
    if not doc_ids:
        return
    marks = ", ".join("?" * len(doc_ids))
    rows = db.con.execute(
        f"SELECT id, filename FROM documents WHERE id IN ({marks}) "
        "  AND missing_since IS NULL "
        "  AND id NOT IN (SELECT doc_id FROM task_docs) ORDER BY id", doc_ids).fetchall()
    first: dict[int, str] = {}
    for row in rows:
        tokens = _tokenize(row["filename"])
        if tokens:
            first[row["id"]] = tokens[0]

    # '용역'과 '용역계약'은 같은 업무의 다른 표기일 수 있다. 가장 짧은 표기로 모은다.
    keys = sorted(set(first.values()), key=lambda key: (len(key), key))
    groups: dict[str, list[int]] = {}
    for doc_id, key in first.items():
        groups.setdefault(next(k for k in keys if key.startswith(k)), []).append(doc_id)

    taken = {row["name"] for row in db.con.execute("SELECT name FROM tasks")}
    for key, members in groups.items():
        name = key[:20]
        if len(members) < min_size or name in taken:
            continue
        group = Cluster(seed_id=members[0], doc_ids=members)   # 응집도 0 → 확신 '낮음'
        task_id = _upsert_task(db, Named(name, description=(
            f"내용만으로는 묶이지 않은 문서 가운데 파일명이 ‘{key}’(으)로 시작하는 "
            f"{len(members)}건입니다. 같은 업무인지 확인해 주세요.")), group)
        result.assigned += _attach(db, task_id, members)
        _store_reading(db, task_id, members)
        result.unassigned -= len(members)
        result.grouped_by_filename += 1


# ── 이름 짓기 ───────────────────────────────────────────────────────

@dataclass(slots=True)
class Named:
    label: str
    description: str | None = None
    source: str = "filename"     # model | filename
    error: str | None = None


def _name(client: OllamaClient | None, names: list[str], tokens: list[str]) -> Named:
    """모델이 없거나 실패하면 파일명 공통 낱말로 대신한다."""
    fallback = _from_tokens(tokens)
    if client is None:
        return Named(fallback)

    naming = name_task(client, names[:MAX_TITLES_FOR_NAMING])
    if naming.error:
        return Named(fallback, error=naming.error)
    if not naming.coherent:
        # 모델이 하나의 업무로 보지 않았다. 억지로 이름을 붙이지 않는다.
        return Named(fallback, description=naming.description)
    return Named(naming.name or fallback, naming.description, source="model")


def _from_tokens(tokens: list[str]) -> str:
    if not tokens:
        return "미확인 업무"
    # 가장 자주 나온 낱말 하나둘로 임시 이름을 만든다.
    return " ".join(tokens[:2])[:20]


def _filenames(db: Database, doc_ids: list[int]) -> list[str]:
    if not doc_ids:
        return []
    marks = ", ".join("?" * len(doc_ids))
    rows = db.con.execute(
        f"SELECT filename FROM documents WHERE id IN ({marks}) ORDER BY eff_date DESC",
        doc_ids,
    ).fetchall()
    return [row["filename"] for row in rows]


# ── 저장 ────────────────────────────────────────────────────────────

def _clear_proposals(db: Database) -> None:
    """이전 제안만 지운다.

    사용자가 승인·수정한 업무와 직접 배정한 문서는 재발견이 덮어쓰지 않는다
    (NFR-SAF-004).

    **업무 자체를 확인하지 않았더라도**, 그 업무의 항목을 고치거나 단계를
    수행했거나 공백을 닫았으면 지우지 않는다. 업무 행을 지우면 그 아래의
    항목·단계·수행 기록이 연쇄로 함께 지워진다 — 후임자가 '이 업무 확인함'을
    누르지 않고 일부터 시작하는 것은 흔한 일이고, 그 일이 다음 분석에서
    사라지면 안 된다. 그런 업무는 문서 배정도 그대로 둔다.
    """
    replaceable = (
        "SELECT id FROM tasks WHERE origin = 'ai' AND status = 'proposed' "
        f"AND id NOT IN ({db.HUMAN_WORK_SQL})"
    )
    db.con.execute(
        f"DELETE FROM task_docs WHERE origin = 'ai' AND task_id IN ({replaceable})")
    db.con.execute(f"DELETE FROM task_reading WHERE task_id IN ({replaceable})")
    db.con.execute(
        f"DELETE FROM tasks WHERE id IN ({replaceable}) "
        "  AND id NOT IN (SELECT task_id FROM task_docs)"
    )


def _upsert_task(db: Database, named: Named, group: Cluster) -> int:
    """같은 이름이 나오면 하나로 합친다.

    한 업무가 두 묶음으로 갈라졌을 때 이름이 같으면 합치는 것이 옳다 —
    업무 분류가 흩어지는 것을 막는 장치다.
    """
    description = named.description or (
        f"파일명과 내용이 비슷한 문서 {group.size}건을 묶었습니다. "
        "이름을 짓지 못해 공통 낱말로 대신했습니다."
    )
    db.con.execute(
        "INSERT INTO tasks(name, description, origin, status, confidence) "
        "VALUES (?, ?, 'ai', 'proposed', ?) ON CONFLICT(name) DO NOTHING",
        (named.label, description, group.confidence),
    )
    row = db.con.execute("SELECT id FROM tasks WHERE name = ?", (named.label,)).fetchone()
    task_id = row["id"]
    # 사람이 고친 업무의 설명은 덮어쓰지 않는다.
    db.con.execute(
        "UPDATE tasks SET description = ? WHERE id = ? AND status = 'proposed'",
        (description, task_id),
    )
    return task_id


def _attach(db: Database, task_id: int, doc_ids: list[int]) -> int:
    """문서를 업무에 붙인다. 사용자가 직접 배정한 문서는 건드리지 않는다."""
    if not doc_ids:
        return 0
    marks = ", ".join("?" * len(doc_ids))
    manual = {
        row["doc_id"]
        for row in db.con.execute(
            f"SELECT doc_id FROM task_docs WHERE origin = 'user' AND doc_id IN ({marks})",
            doc_ids,
        )
    }
    targets = [doc_id for doc_id in doc_ids if doc_id not in manual]
    db.con.executemany(
        "INSERT INTO task_docs(task_id, doc_id, origin, confidence) "
        "VALUES (?, ?, 'ai', 'medium') ON CONFLICT(task_id, doc_id) DO NOTHING",
        [(task_id, doc_id) for doc_id in targets],
    )
    return len(targets)


def _store_reading(db: Database, task_id: int, doc_ids: list[int]) -> None:
    """먼저 읽을 문서와 그 이유를 저장한다."""
    facts = _facts(db, doc_ids)
    picks = scoring.recommend(facts, limit=5)
    db.con.execute("DELETE FROM task_reading WHERE task_id = ?", (task_id,))
    db.con.executemany(
        "INSERT INTO task_reading(task_id, doc_id, ordinal, score, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (task_id, pick.doc_id, ordinal, pick.score, pick.reason)
            for ordinal, pick in enumerate(picks, start=1)
        ],
    )


def _facts(db: Database, doc_ids: list[int]) -> list[scoring.DocFacts]:
    if not doc_ids:
        return []
    marks = ", ".join("?" * len(doc_ids))
    # 완전 중복본은 대표 하나만 남긴다. 같은 문서를 두 번 추천하면
    # 추천 목록 다섯 칸 중 두 칸이 같은 것으로 채워진다.
    rows = db.con.execute(
        f"""
        SELECT d.id, d.filename, d.eff_year, d.eff_month, d.eff_date_kind,
               d.char_count,
               (SELECT COUNT(*) FROM documents x
                 WHERE x.hash = d.hash AND d.hash IS NOT NULL
                   AND x.missing_since IS NULL) AS dup_n,
               EXISTS(SELECT 1 FROM version_groups g WHERE g.latest_doc_id = d.id) AS is_rep
        FROM documents d
        WHERE d.id IN ({marks}) AND d.missing_since IS NULL
          AND (d.hash IS NULL OR d.id = (
                SELECT MIN(y.id) FROM documents y
                WHERE y.hash = d.hash AND y.missing_since IS NULL))
        """,
        doc_ids,
    ).fetchall()
    return [
        scoring.DocFacts(
            doc_id=row["id"],
            filename=row["filename"],
            eff_year=row["eff_year"],
            eff_month=row["eff_month"],
            date_kind=row["eff_date_kind"],
            duplicate_count=max(1, row["dup_n"] or 1),
            char_count=row["char_count"] or 0,
            is_version_representative=bool(row["is_rep"]),
        )
        for row in rows
    ]
