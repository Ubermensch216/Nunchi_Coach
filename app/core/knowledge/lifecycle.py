"""영속 큐의 lease·회수·취소. 모델 호출 없이 파티션 하나씩 재검증한다."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

from ...db.knowledge import KnowledgeStore

MAX_ATTEMPTS = 3
LEASE_SECONDS = 120


class ObsoleteJob(RuntimeError):
    pass


class CancelledJob(RuntimeError):
    pass


@dataclass
class RunReport:
    completed: int = 0
    failed: int = 0
    obsolete: int = 0
    cancelled: bool = False
    diagnostics: list[str] = field(default_factory=list)


def schedule_document(db, doc_id: int, reason: str = "notebook_ingest") -> None:
    KnowledgeStore(db).schedule(f"document:{doc_id}", reason)


def schedule_tasks(db, reason: str = "restored") -> None:
    store = KnowledgeStore(db)
    with db.transaction():
        for row in db.con.execute("SELECT id FROM tasks"):
            store.schedule(f"task:{row[0]}", reason)


def recover(db, *, now=None) -> int:
    """다른 프로세스의 살아 있는 lease는 건드리지 않는다."""
    now = time.time() if now is None else now
    return db.con.execute("UPDATE kg_jobs SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'dirty' END,"
        "lease=NULL,lease_until=NULL,error='interrupted lease' "
        "WHERE state='running' AND (lease_until IS NULL OR lease_until<=?)", (MAX_ATTEMPTS, now)).rowcount


def acquire(db, *, now=None):
    now = time.time() if now is None else now
    with db.transaction():
        recover(db, now=now)
        return db.con.execute("UPDATE kg_jobs SET state='running',lease=?,lease_until=?,"
            "leased_revision=wanted_revision,attempts=attempts+1 WHERE (partition_key,job_kind)=("
            "SELECT partition_key,job_kind FROM kg_jobs WHERE state='dirty' AND attempts<? "
            "AND job_kind='projection' ORDER BY CASE WHEN partition_key='project' THEN 0 ELSE 1 END,partition_key LIMIT 1) "
            "RETURNING *", (str(uuid4()), now + LEASE_SECONDS, MAX_ATTEMPTS)).fetchone()


def cancel(db, partition: str) -> None:
    db.con.execute("UPDATE kg_jobs SET state='cancelled',lease=NULL,lease_until=NULL "
                   "WHERE partition_key=? AND job_kind='projection'", (partition,))


def resume(db, partition: str | None = None) -> int:
    return db.con.execute("UPDATE kg_jobs SET state='dirty',lease=NULL,lease_until=NULL,attempts=0,error='' "
        "WHERE state IN ('cancelled','failed') AND (? IS NULL OR partition_key=?)", (partition, partition)).rowcount


def pending(db) -> bool:
    return bool(db.con.execute("SELECT 1 FROM kg_jobs WHERE job_kind='projection' AND "
        "((state='dirty' AND attempts<?) OR (state='running' AND coalesce(lease_until,0)<=?)) LIMIT 1",
        (MAX_ATTEMPTS, time.time())).fetchone())


def _expand_project(db, job):
    """전체 예약도 문서/업무별 큐로 나눈다. 삭제된 기존 파티션을 포함한다."""
    with db.transaction():
        KnowledgeStore(db).check_lease('project')
        keys = {r[0] for r in db.con.execute("SELECT partition_key FROM kg_jobs WHERE partition_key!='project'")}
        for table, prefix in (('documents', 'document'), ('tasks', 'task')):
            keys.update(f'{prefix}:{r[0]}' for r in db.con.execute(f'SELECT id FROM {table}'))
        store = KnowledgeStore(db)
        for key in sorted(keys):
            store.schedule(key, 'project_expand')
        db.con.execute("UPDATE kg_jobs SET state='done',lease=NULL,lease_until=NULL,error='' "
                       "WHERE partition_key='project' AND lease=?", (job['lease'],))


def run_jobs(db, *, should_stop=lambda: False, max_jobs=100, seconds=5) -> RunReport:
    from .projection import project_document, project_task

    report = RunReport()
    deadline = time.monotonic() + seconds
    for _ in range(max_jobs):
        if should_stop() or time.monotonic() >= deadline:
            report.cancelled = should_stop()
            break
        job = acquire(db)
        if job is None:
            break
        key, token = job['partition_key'], job['lease']
        db._kg_lease = (token, job['leased_revision'])
        db._kg_should_stop = should_stop
        try:
            if key == 'project':
                _expand_project(db, job)
            else:
                kind, raw_id = key.split(':', 1)
                projector = {'document': project_document, 'task': project_task}[kind]
                result = projector(db, int(raw_id))
                report.diagnostics.extend(result.diagnostics)
            report.completed += 1
        except CancelledJob:
            # 앱 종료/분석 중지는 다음 실행에 자동 재개할 수 있도록 dirty로 반환한다.
            db.con.execute("UPDATE kg_jobs SET state='dirty',lease=NULL,lease_until=NULL,attempts=max(attempts-1,0) "
                           "WHERE partition_key=? AND lease=?", (key, token))
            report.cancelled = True
            break
        except ObsoleteJob:
            db.con.execute("UPDATE kg_jobs SET state='dirty',lease=NULL,lease_until=NULL,attempts=0 "
                           "WHERE partition_key=? AND lease=?", (key, token))
            report.obsolete += 1
        except Exception as exc:
            error = f'{type(exc).__name__}: {str(exc)[:500]}'
            report.failed += 1
            report.diagnostics.append(f'{key}: {error}')
            db.con.execute("UPDATE kg_jobs SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'dirty' END,"
                "lease=NULL,lease_until=NULL,error=? WHERE partition_key=? AND lease=?",
                (MAX_ATTEMPTS, error, key, token))
        finally:
            db._kg_lease = None
            db._kg_should_stop = lambda: False
    return report
