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
import json
import re

from ..core import docrole, folders, scoring, discovery_index
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
    singleton_candidates: int = 0
    held_groups: int = 0
    cancelled: bool = False
    named_by_folder: int = 0         # 전임자의 폴더 구조로 찾은 업무
    named_by_official: int = 0       # 온나라 공문으로 세운 업무
    matched_to_official: int = 0     # 공문 업무에 붙인 PC 파일

    def summary(self) -> str:
        parts = [f"업무 {self.tasks}개", f"배정 {self.assigned:,}건"]
        if self.named_by_official:
            parts.append(f"공문으로 세운 업무 {self.named_by_official}개")
        if self.matched_to_official:
            parts.append(f"공문 업무에 붙인 PC 파일 {self.matched_to_official:,}건")
        if self.named_by_folder:
            parts.append(f"폴더 구조로 찾은 업무 {self.named_by_folder}개")
        if self.grouped_by_filename:
            parts.append(f"파일명으로 묶은 제안 {self.grouped_by_filename}개")
        if self.unassigned:
            parts.append(f"미분류 {self.unassigned:,}건")
        if self.named_by_filename:
            parts.append(f"파일명으로 이름 지음 {self.named_by_filename}개")
        if self.singleton_candidates:
            parts.append(f"단일 문서 확인 후보 {self.singleton_candidates}개")
        if self.held_groups:
            parts.append(f"동일 업무 판단 보류 {self.held_groups}개")
        return " · ".join(parts)


class _Cancelled(Exception):
    pass


def discover(db, client=None, threshold=None, min_size=None, on_progress=None) -> Discovery:
    # Failed/cancelled rediscovery must not erase the previous proposals.
    try:
        return _discover(db, client, threshold, min_size, on_progress)
    except _Cancelled:
        return Discovery(tasks=len(db.tasks()), unassigned=db.unclassified_count(), cancelled=True,
                         errors=["업무 발견을 중단했습니다. 이전 제안을 보존했습니다."])


def _discover(
    db: Database,
    client: OllamaClient | None = None,
    threshold: float | None = None,
    min_size: int | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> Discovery:
    result = Discovery()
    revision = (db.get_meta("content_revision"), db.get_meta("chunking_version"))

    model = client.embed_model if client else db.get_meta("embed_model") or "bge-m3"
    store, contributions = discovery_index.load(db, model)
    protected = _protected_documents(db)
    # 온나라 공문이 있으면 그것이 업무의 뼈대다. 공문 업무가 가져간 문서(공문과, 공문에
    # 대응된 PC 파일)는 아래의 폴더·본문 묶기에서 뺀다 — 남은 문서만 지금까지처럼 찾는다.
    official = _official_plan(db, protected)
    excluded = protected | {doc_id for task in official for doc_id in task.claimed}
    if store.size:
        import numpy as np
        keep = np.asarray([int(doc) not in excluded for doc in store.ids])
        store = VectorStore(store.ids[keep], store.matrix[keep])
    # 전임자가 폴더로 일을 정리해 두었으면 그 구조가 다음 단서다. 본문이
    # 닮았는지는 그다음이다. 폴더가 평평하면 plan이 없고, 아래의 본문 묶기로 간다.
    plan = _folder_plan(db, excluded)
    if store.size == 0 and plan is None:
        if official:
            with db.transaction():
                return _save_official_only(db, result, official)
        result.tasks = len(db.tasks())
        result.unassigned = db.unclassified_count()
        if not protected:
            result.errors.append("의미 색인이 없습니다. 먼저 분석을 끝내세요.")
        return result

    if plan is not None:
        with db.transaction():
            return _save_folder_discovery(db, result, plan, store, contributions, model, official)

    kwargs = {}
    if threshold is not None:
        kwargs["threshold"] = threshold
    if min_size is not None:
        kwargs["min_size"] = min_size
    grouping = cluster(store, **kwargs)

    result.clusters = len(grouping.clusters)
    result.unassigned = len(grouping.unassigned)

    naming_ready = bool(client and client.health().generation_ready)
    prepared = []
    # Model calls hold no write transaction: the user can still correct tasks.
    for index, group in enumerate(grouping.clusters, start=1):
        if client is not None and getattr(client, "cancelled", False):
            raise _Cancelled()
        names = _filenames(db, group.doc_ids)
        tokens = common_tokens(names)

        excerpts = discovery_index.naming_evidence(db, group.doc_ids, contributions)
        named = _name(client if naming_ready else None, names, tokens, excerpts)
        if client is not None and getattr(client, "cancelled", False):
            raise _Cancelled()
        prepared.append((group, named, excerpts))
        if on_progress:
            on_progress(index, len(grouping.clusters), named.label)
    with db.transaction():
        current = (db.get_meta("content_revision"), db.get_meta("chunking_version"))
        if current != revision or _protected_documents(db) != protected:
            return Discovery(tasks=len(db.tasks()), unassigned=db.unclassified_count(),
                             errors=["검토 중 자료나 사용자 교정이 변경되어 이전 제안을 보존했습니다. 다시 실행해 주세요."])
        return _save_discovery(db, client, result, grouping, store, contributions, model,
                               prepared, naming_ready, protected, min_size, official)


def _save_discovery(db, client, result, grouping, store, contributions, model,
                    prepared, naming_ready, protected, min_size, official=()):
    if client is not None and getattr(client, "cancelled", False):
        raise _Cancelled()
    _clear_proposals(db)
    db.con.execute("DELETE FROM discovery_holds")
    _save_official(db, result, official)
    reviewed: set[int] = set()
    held: set[int] = set()
    for group, named, excerpts in prepared:
        if naming_ready and not named.error:
            reviewed.update(e["chunk_id"] for e in excerpts)
        if named.coherent is False:
            db.con.execute("INSERT INTO discovery_holds(signature,doc_ids,reason) VALUES(?,?,?)",
                           (",".join(map(str, sorted(group.doc_ids))), json.dumps(group.doc_ids),
                            "coherence_rejected"))
            held.update(group.doc_ids)
            result.held_groups += 1
            continue
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

    if not (client is not None and getattr(client, "cancelled", False)):
        _group_leftovers(db, grouping.unassigned, min_size or DEFAULT_MIN_SIZE, result)
        # A singleton is an explicitly weak candidate, only when real body evidence exists.
        for doc_id in grouping.unassigned:
            if doc_id in protected or doc_id in held:
                continue
            if db.con.execute("SELECT 1 FROM task_docs WHERE doc_id=?", (doc_id,)).fetchone():
                continue
            body = db.con.execute("SELECT id,locator FROM chunks WHERE doc_id=? AND trim(text)!='' "
                                  "ORDER BY ordinal LIMIT 1", (doc_id,)).fetchone()
            if not body:
                continue
            names = _filenames(db, [doc_id])
            tokens = _tokenize(names[0]) if names else []
            group = Cluster(seed_id=doc_id, doc_ids=[doc_id])
            task_id = _upsert_task(db, Named(_from_tokens(tokens), description=(
                "본문 근거가 있는 단일 문서 후보입니다. 독립 업무인지 참고자료인지 확인해 주세요.")), group)
            _attach(db, task_id, [doc_id])
            _store_reading(db, task_id, [doc_id])
            result.singleton_candidates += 1
    else:
        raise _Cancelled()

    for row in db.con.execute("SELECT td.task_id,td.doc_id FROM task_docs td "
                              "JOIN tasks t ON t.id=td.task_id WHERE t.origin='ai' AND t.status='proposed'"):
        doc_id = row["doc_id"]
        if doc_id in protected:
            continue
        for piece in contributions.get(doc_id, []):
            db.con.execute(
                "INSERT OR REPLACE INTO discovery_evidence(task_id,doc_id,piece_key,chunk_id,doc_hash,locator,"
                "basis,score,model,index_version,reviewed_by_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (row["task_id"], doc_id, str(piece["chunk_id"] or "prefix"), piece["chunk_id"], piece["doc_hash"], piece["locator"],
                 piece["basis"], piece["score"], model, discovery_index.VERSION,
                 int(piece["chunk_id"] in reviewed)))

    candidates = set(map(int, store.ids))
    assigned = {r[0] for r in db.con.execute("SELECT DISTINCT td.doc_id FROM task_docs td "
                "JOIN tasks t ON t.id=td.task_id WHERE t.not_a_task=0 AND t.merged_into IS NULL")}
    # 공문 업무가 가져간 문서는 색인 후보에서 이미 뺐다. 배정 건수에는 넣는다.
    result.assigned = len(candidates & assigned) + len(
        {doc_id for task in official for doc_id in task.claimed} & assigned)
    result.unassigned = len(candidates - assigned)

    result.tasks = db.con.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    db.audit("task.discover", detail=result.summary(), result="ok")
    return result


def _protected_documents(db: Database) -> set[int]:
    rows = db.con.execute(
        "SELECT td.doc_id FROM task_docs td JOIN tasks t ON t.id=td.task_id "
        "WHERE td.origin='user' OR t.origin='user' OR t.status!='proposed' "
        "OR t.not_a_task=1 OR t.merged_into IS NOT NULL "
        f"OR t.id IN ({db.HUMAN_WORK_SQL}) "
        "UNION SELECT doc_id FROM corrections WHERE target='task_docs' AND after_val IS NULL "
        "AND doc_id IS NOT NULL "
        # 압축 안의 문서는 업무 찾기에 끼지 않는다 — 부모 압축의 업무를 따른다(ingest/members.py).
        # 끼면 압축 이름이 업무 폴더로 잡힌다.
        "UNION SELECT id FROM documents WHERE parent_id IS NOT NULL")
    return {r[0] for r in rows}


# ── 폴더 구조로 업무 찾기 ───────────────────────────────────────────
# 사람이 전임자 폴더를 받아 들고 하는 일을 그대로 한다.
#   1. 맨 위 폴더(연도·old 묶음은 지나쳐서)를 업무로 본다.
#   2. 이름의 알맹이가 같은 폴더는 한 업무다. '2024년 적극행정 실행계획'과
#      '[0. old]\[2023년도]\[적극행정 실행계획]'은 같은 일의 다른 회차다.
#   3. 폴더에 들지 않은 낱장은 이름에 업무 이름이 들어 있을 때만 그 업무에 붙인다.
#      아니면 미분류로 둔다 — 확신 없는 문서를 업무에 밀어 넣지 않는다.
# 본문을 읽지 못한 문서(스캔 PDF 등)도 폴더가 알려 주는 업무에는 붙인다. 내용은
# 몰라도 그 일의 자료라는 것은 전임자가 이미 정해 두었다.

from ..core import bundles  # noqa: E402

FOLDER_MIN_DOCS = 2        # 문서가 이만큼은 있어야 폴더를 업무로 올린다
FOLDER_MODE_SHARE = 0.5    # 문서의 절반 이상이 업무 폴더 안에 있어야 폴더를 믿는다
FOLDER_EVIDENCE = "폴더 위치"
NAME_EVIDENCE = "파일 이름에 업무 이름"


@dataclass(slots=True)
class FolderTask:
    name: str
    cores: list[str]                       # 이 업무로 합친 폴더 이름의 알맹이들
    folders: list[str] = field(default_factory=list)
    rounds: list[str] = field(default_factory=list)
    doc_ids: list[int] = field(default_factory=list)       # 폴더로 정한 문서
    named_ids: list[int] = field(default_factory=list)     # 이름으로 붙인 낱장
    unread: set[int] = field(default_factory=set)
    round_of: dict[int, str] = field(default_factory=dict)  # 문서 → 회차('2024 하반기')
    not_documents: int = 0                 # 이 폴더의 그림·글꼴 등 문서로 다루지 않은 파일 수
    loose: bool = False                    # 업무 폴더 밖의 낱장을 모은 묶음(core/bundles.py)


def _folder_plan(db: Database, protected: set[int]) -> list[FolderTask] | None:
    # 온나라 수집본은 뺀다. 그 폴더(files/<해시>)는 수집기가 만든 것이지 전임자가 정리한 구조가
    # 아니다 — 넣으면 공문 한 건(본문+붙임)이 해시 이름의 업무가 된다. 공문은 공문 업무 찾기가 맡는다.
    rows = db.con.execute(
        "SELECT d.id, d.path, d.filename, d.parse_status, s.id AS source_id, s.path AS root "
        "FROM documents d JOIN sources s ON s.id = d.source_id AND s.kind NOT IN ('upload', 'onnara') "
        "WHERE d.missing_since IS NULL AND d.parse_status NOT IN ('skipped', 'pending') "
        "ORDER BY d.id").fetchall()
    rows = [row for row in rows if row["id"] not in protected]
    if not rows:
        return None

    trees: dict[int, folders.Tree] = {}
    for row in rows:
        trees.setdefault(row["source_id"], folders.Tree(row["root"]))
    by_source: dict[int, list[str]] = {}
    for row in rows:
        by_source.setdefault(row["source_id"], []).append(row["path"])
    for source_id, paths in by_source.items():
        trees[source_id] = folders.Tree.build(trees[source_id].root, paths)

    groups: dict[tuple, list] = {}
    loose = []
    for row in rows:
        placement = trees[row["source_id"]].place(row["path"])
        if placement.loose:
            loose.append(row)
        else:
            groups.setdefault((row["source_id"], placement.task_parts), []).append((row, placement))
    foldered = sum(len(members) for members in groups.values())
    if len(groups) < 2 or foldered < len(rows) * FOLDER_MODE_SHARE:
        return None

    # 업무 폴더 안의, 문서로 다루지 않은 파일(사진·글꼴 등). 업무에 붙이지는 않지만
    # 있다는 사실은 알린다 — 행사 사진이 수십 장 있는 폴더를 '문서 12건'이라고만
    # 말하면 후임자가 폴더를 열었을 때 본 것과 다르다.
    skipped: dict[tuple, int] = {}
    for row in db.con.execute(
            "SELECT d.path, s.id AS source_id FROM documents d JOIN sources s ON s.id = d.source_id "
            "AND s.kind NOT IN ('upload', 'onnara') WHERE d.missing_since IS NULL AND d.parse_status = 'skipped'"):
        if row["source_id"] in trees:
            placement = trees[row["source_id"]].place(row["path"])
            if not placement.loose:
                key = (row["source_id"], placement.task_parts)
                skipped[key] = skipped.get(key, 0) + 1

    labels = {key: members[0][1].label for key, members in groups.items()}
    common = folders.common_tokens(list(labels.values()))
    cores = {key: folders.core(label, common) for key, label in labels.items()}
    target = _merge_targets(sorted(set(cores.values())))

    merged: dict[str, list[tuple]] = {}
    for key in groups:
        merged.setdefault(target[cores[key]], []).append(key)

    tasks: list[FolderTask] = []
    for canonical, keys in merged.items():
        members = [item for key in keys for item in groups[key]]
        if len(members) < FOLDER_MIN_DOCS:
            loose.extend(row for row, _ in members)
            continue
        # 이름은 알맹이가 같은 폴더의 것. 여럿이면 문서가 많은 폴더의 것.
        named = max(keys, key=lambda key: (cores[key] == canonical, len(groups[key])))
        task = FolderTask(name=labels[named][:60], cores=sorted({cores[key] for key in keys}, key=len))
        task.folders = ["\\".join(key[1]) for key in sorted(keys, key=lambda key: -len(groups[key]))]
        task.rounds = sorted({placement.round_key for _, placement in members if placement.round_key})
        task.doc_ids = [row["id"] for row, _ in members]
        task.unread = {row["id"] for row, _ in members if row["parse_status"] not in ("ok", "partial")}
        task.round_of = {row["id"]: placement.round_key for row, placement in members}
        task.not_documents = sum(skipped.get(key, 0) for key in keys)
        tasks.append(task)
    if len(tasks) < 2:
        return None

    left = []
    for row in loose:
        owner = _owner_by_name(row["filename"], tasks, common)
        if owner is not None:
            owner.named_ids.append(row["id"])
            owner.round_of[row["id"]] = folders.parse_part(row["filename"].rsplit(".", 1)[0]).round_key
            if row["parse_status"] not in ("ok", "partial"):
                owner.unread.add(row["id"])
        else:
            left.append(row)
    # 어느 업무에도 붙지 않은 낱장은 한데 모아 보인다. 미분류로만 두면 구·군 담당자 명단,
    # 분기 제출 건수 같은 자료가 어디에도 나타나지 않는다. 일은 아니다 — 순서·주기·공백을
    # 만들지 않는다(core/bundles.py).
    if len(left) >= bundles.LOOSE_MIN:
        bundle = FolderTask(name=bundles.LOOSE_NAME, cores=[], loose=True)
        bundle.doc_ids = [row["id"] for row in left]
        bundle.unread = {row["id"] for row in left if row["parse_status"] not in ("ok", "partial")}
        tasks.append(bundle)
    tasks.sort(key=lambda task: -(len(task.doc_ids) + len(task.named_ids)))
    return tasks


def has_folder_tasks(db: Database) -> bool:
    """폴더 구조만으로 업무를 찾을 수 있는 자료인가(의미 색인이 없어도 된다)."""
    return _folder_plan(db, _protected_documents(db)) is not None


def _merge_targets(cores: list[str]) -> dict[str, str]:
    """알맹이가 다른 알맹이를 품고 있으면 품긴 쪽으로 합친다.

    '중점과제선정계획'·'중점과제추진결과보고'는 '중점과제'의 일이다. 둘 이상을 품으면
    뒤에 나온 것을 고른다 — 우리말은 뒤가 중심어라 '위원회우수공무원선발'은 위원회가
    아니라 우수공무원 선발이다.
    """
    target = {core: core for core in cores}
    for core in sorted(cores, key=len):
        inside = [(core.rfind(other), len(other), other) for other in cores
                  if other != core and len(other) >= 2 and len(other) < len(core) and other in core]
        if inside:
            target[core] = target[max(inside)[2]]
    return target


def _owner_by_name(filename: str, tasks: list[FolderTask], common: list[str]) -> FolderTask | None:
    """낱장의 이름에 업무 이름이 들어 있으면 그 업무. 둘 이상이면 더 긴 이름 쪽."""
    from pathlib import Path

    stem = folders.compact(Path(filename).stem)
    hits = [(hit, index) for index, task in enumerate(tasks)
            if (hit := max((len(core) for core in task.cores if len(core) >= 2 and core in stem),
                           default=0))]
    if not hits:
        # 띄어 쓴 이름('소송 지원 관련')은 낱말 둘 이상이 겹치면 같은 일로 본다.
        for index, task in enumerate(tasks):
            words = {word for word in task.name.split() if len(word) >= 2 and word not in common}
            shared = [word for word in words if word in stem]
            if len(shared) >= 2:
                hits.append((sum(map(len, shared)), index))
    if not hits:
        return None
    hits.sort(reverse=True)
    if len(hits) > 1 and hits[0][0] == hits[1][0]:
        return None      # 비기면 정하지 않는다
    return tasks[hits[0][1]]


def _save_folder_discovery(db, result, plan, store, contributions, model, official=()) -> Discovery:
    _clear_proposals(db)
    db.con.execute("DELETE FROM discovery_holds")
    _save_official(db, result, official)
    for task in plan:
        every = task.doc_ids + task.named_ids
        places = len(task.folders)
        if task.loose:
            description = f"{bundles.NOTE[bundles.LOOSE]} 문서 {len(every):,}건입니다."
        else:
            where = f"‘{task.folders[0]}’ 폴더" + (f" 등 {places}곳" if places > 1 else "")
            description = f"{where}의 문서 {len(every):,}건입니다."
        if task.rounds:
            description += " 회차: " + " · ".join(task.rounds[:8]) + "."
        if task.named_ids:
            description += f" 폴더 밖 문서 {len(task.named_ids)}건은 이름을 보고 붙였습니다."
        if task.unread:
            description += f" 본문을 읽지 못한 문서 {len(task.unread)}건은 위치만 보고 붙였습니다."
        if task.not_documents:
            description += (f" 이 폴더에는 사진·그림 등 문서로 다루지 않은 파일이 "
                            f"{task.not_documents}개 더 있습니다.")
        group = Cluster(seed_id=min(every), doc_ids=every)
        task_id = _upsert_task(db, Named(task.name, description), group, confidence="medium")
        read = [doc_id for doc_id in task.doc_ids if doc_id not in task.unread]
        placed = bundles.LOOSE_EVIDENCE if task.loose else FOLDER_EVIDENCE
        result.assigned += _attach(db, task_id, read, evidence=placed, rounds=task.round_of)
        _attach(db, task_id, [d for d in task.named_ids if d not in task.unread],
                evidence=NAME_EVIDENCE, rounds=task.round_of)
        _attach(db, task_id, sorted(task.unread), confidence="low", evidence=placed,
                rounds=task.round_of)
        _store_reading(db, task_id, every)
        result.named_by_folder += 1

    for row in db.con.execute("SELECT td.task_id,td.doc_id FROM task_docs td "
                              "JOIN tasks t ON t.id=td.task_id WHERE t.origin='ai' AND t.status='proposed'"):
        for piece in contributions.get(row["doc_id"], []):
            db.con.execute(
                "INSERT OR REPLACE INTO discovery_evidence(task_id,doc_id,piece_key,chunk_id,doc_hash,locator,"
                "basis,score,model,index_version,reviewed_by_model) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                (row["task_id"], row["doc_id"], str(piece["chunk_id"] or "prefix"), piece["chunk_id"],
                 piece["doc_hash"], piece["locator"], piece["basis"], piece["score"], model,
                 discovery_index.VERSION))

    result.clusters = len(plan)
    result.assigned = db.con.execute(
        "SELECT COUNT(DISTINCT td.doc_id) FROM task_docs td JOIN tasks t ON t.id=td.task_id "
        "WHERE t.not_a_task=0 AND t.merged_into IS NULL").fetchone()[0]
    result.unassigned = db.unclassified_count()
    result.tasks = db.con.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    db.audit("task.discover", detail=result.summary(), result="ok")
    return result


# ── 공문으로 업무 찾기 ──────────────────────────────────────────────
# 공문은 기관이 등록한 기록이라 폴더보다 먼저 믿는다(docs/공문기준_업무분석_계획서 P3·P4a).
#   1. 단위과제가 같은 공문은 한 업무다. 단위과제가 다른 공문은 합치지 않는다.
#   2. 제목의 알맹이(core/official.py)가 같거나 한쪽이 다른 쪽을 품으면 한 업무다.
#   3. 본문이 다른 공문의 문서번호를 적었으면(접수 → 회신) 한 업무다.
#   4. 공문 2건 이상이거나 단위과제로 묶인 것만 업무로 올린다. 1건짜리 접수 공문이
#      업무 수백 개로 불어나지 않게, 나머지는 아래의 폴더·본문 묶기에 맡긴다.
# 그다음 PC 파일을 붙인다. 앞의 근거가 이기고, 두 업무에 같은 무게로 걸리면 붙이지 않는다.

from ..core import official as official_docs  # noqa: E402

OFFICIAL_MIN_RECORDS = 2
OFFICIAL_EVIDENCE = "온나라 공문"
SAME_EVIDENCE = "공문 파일과 같은 내용"
NUMBER_EVIDENCE = "파일 이름에 공문 문서번호"
TITLE_EVIDENCE = "파일 이름에 공문 제목"
NEAR_EVIDENCE = "공문과 대응된 파일과 같은 폴더"


@dataclass(slots=True)
class OfficialTask:
    name: str
    basis: str                             # 단위과제 | 공문 제목
    records: list[official_docs.Record]
    cores: list[str] = field(default_factory=list)
    local: dict[int, tuple[str, str]] = field(default_factory=dict)  # PC 파일 → (확신, 근거)
    round_of: dict[int, str] = field(default_factory=dict)
    protected: set[int] = field(default_factory=set)   # 사람이 다른 업무에 둔 공문 파일

    @property
    def official_ids(self) -> list[int]:
        return sorted({doc_id for record in self.records for doc_id in record.doc_ids} - self.protected)

    @property
    def claimed(self) -> set[int]:
        return set(self.official_ids) | set(self.local)


def _official_plan(db: Database, protected: set[int]) -> list[OfficialTask]:
    records = official_docs.records(db)
    if len(records) < OFFICIAL_MIN_RECORDS and not any(r.task_card for r in records):
        return []

    parent = list(range(len(records)))
    card = [folders.compact(record.task_card) for record in records]

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(left: int, right: int) -> None:
        left, right = root(left), root(right)
        if left == right or (card[left] and card[right] and card[left] != card[right]):
            return      # 단위과제가 다른 공문은 기관이 다른 업무로 정했다
        parent[right] = left
        card[left] = card[left] or card[right]

    first: dict[str, int] = {}
    for index, record in enumerate(records):
        if record.task_card:
            join(first.setdefault("card:" + card[index], index), index)
    cores = [official_docs.title_core(record.title) for record in records]
    target = _merge_targets(sorted({core for core in cores if core}))
    for index, core in enumerate(cores):
        if core:
            join(first.setdefault("core:" + target[core], index), index)
    numbers = {official_docs.normalize_number(record.document_number): index
               for index, record in enumerate(records) if record.document_number}
    for index, record in enumerate(records):
        text = official_docs.body_text(db, record.body_ids)
        for number in official_docs.numbers_in(text):
            if number in numbers and numbers[number] != index:
                join(numbers[number], index)

    members: dict[int, list[int]] = {}
    for index in range(len(records)):
        members.setdefault(root(index), []).append(index)
    tasks: list[OfficialTask] = []
    for head, indexes in members.items():
        if len(indexes) < OFFICIAL_MIN_RECORDS and not card[head]:
            continue
        group = [records[index] for index in indexes]
        group_cores = sorted({cores[index] for index in indexes if cores[index]}, key=len)
        if card[head]:
            named = next(record.task_card for record in group if record.task_card)
            basis = "단위과제"
        else:
            # 이름은 가장 짧은 알맹이를 가진 공문들에서 가장 흔한 제목.
            shortest = group_cores[0] if group_cores else ""
            labels = [official_docs.title_label(records[index].title) for index in indexes
                      if shortest and cores[index] == shortest]
            named = max(labels, key=labels.count) if labels else group[0].title
            basis = "공문 제목"
        task = OfficialTask(name=named[:60], basis=basis, records=group, cores=group_cores,
                            protected={doc_id for record in group for doc_id in record.doc_ids} & protected)
        for record in group:
            for doc_id in record.doc_ids:
                task.round_of[doc_id] = record.year
        tasks.append(task)
    if not tasks:
        return []
    _match_local(db, tasks, protected)
    tasks.sort(key=lambda task: -len(task.claimed))
    return tasks


def _match_local(db: Database, tasks: list[OfficialTask], protected: set[int]) -> None:
    """PC 파일(온나라 수집본이 아닌 문서)을 공문 업무에 붙인다."""
    from pathlib import Path

    rows = [row for row in db.con.execute(
        "SELECT d.id, d.path, d.filename, d.hash, d.eff_year, s.path AS root "
        "FROM documents d JOIN sources s ON s.id = d.source_id AND s.kind NOT IN ('upload', 'onnara') "
        "WHERE d.missing_since IS NULL AND d.parse_status NOT IN ('skipped', 'pending') "
        "ORDER BY d.id") if row["id"] not in protected]
    by_hash: dict[str, set[int]] = {}
    by_number: dict[str, set[int]] = {}
    for index, task in enumerate(tasks):
        for record in task.records:
            for digest in record.hashes:
                by_hash.setdefault(digest, set()).add(index)
            if record.document_number:
                by_number.setdefault(official_docs.normalize_number(record.document_number), set()).add(index)

    def only(owners: set[int]) -> int | None:
        return next(iter(owners)) if len(owners) == 1 else None

    def by_title(stem: str) -> int | None:
        hits = sorted(((max((len(core) for core in task.cores if core in stem), default=0), index)
                       for index, task in enumerate(tasks)), reverse=True)
        if not hits or not hits[0][0] or (len(hits) > 1 and hits[0][0] == hits[1][0]):
            return None     # 비기면 정하지 않는다
        return hits[0][1]

    placed: dict[int, int] = {}
    for row in rows:
        stem = re.sub(r"[^\w]", "", folders.compact(Path(row["filename"]).stem))
        found = None
        if row["hash"] and (owner := only(by_hash.get(row["hash"], set()))) is not None:
            found = (owner, "high", SAME_EVIDENCE)
        elif (owner := only({task for number in official_docs.numbers_in(row["filename"])
                             for task in by_number.get(number, set())})) is not None:
            found = (owner, "high", NUMBER_EVIDENCE)
        elif (owner := by_title(stem)) is not None:
            found = (owner, "medium", TITLE_EVIDENCE)
        if found:
            owner, confidence, evidence = found
            tasks[owner].local[row["id"]] = (confidence, evidence)
            placed[row["id"]] = owner

    # 대응된 파일과 같은 폴더의 나머지 — 그 폴더의 대응이 한 업무뿐일 때만. 자료 폴더의 맨 위는
    # 전임자가 아무 파일이나 두는 곳이라 쓰지 않는다.
    folder_of = {row["id"]: str(Path(row["path"]).parent) for row in rows}
    owners: dict[str, set[int]] = {}
    for doc_id, owner in placed.items():
        owners.setdefault(folder_of[doc_id], set()).add(owner)
    for row in rows:
        if row["id"] in placed or folder_of[row["id"]] == str(Path(row["root"])):
            continue
        owner = only(owners.get(folder_of[row["id"]], set()))
        if owner is not None:
            tasks[owner].local[row["id"]] = ("low", NEAR_EVIDENCE)
    years = {row["id"]: row["eff_year"] for row in rows}
    for task in tasks:
        for doc_id in task.local:
            task.round_of[doc_id] = str(years.get(doc_id) or "")


def has_official_tasks(db: Database) -> bool:
    """공문만으로 업무를 세울 수 있는 자료인가(의미 색인이 없어도 된다)."""
    return bool(_official_plan(db, _protected_documents(db)))


def _save_official(db: Database, result: Discovery, plan) -> None:
    for task in plan:
        official_ids = task.official_ids
        years = sorted({record.year for record in task.records if record.year})
        span = f"{years[0]}~{years[-1]}" if len(years) > 1 else (years[0] if years else "일자 미상")
        description = f"온나라 공문 {len(task.records)}건({span})을 {task.basis}(으)로 묶었습니다."
        if task.local:
            counts = {evidence: sum(1 for _, e in task.local.values() if e == evidence)
                      for evidence in (SAME_EVIDENCE, NUMBER_EVIDENCE, TITLE_EVIDENCE, NEAR_EVIDENCE)}
            reasons = ", ".join(f"{evidence} {n}건" for evidence, n in counts.items() if n)
            description += f" 이 PC의 파일 {len(task.local)}건을 붙였습니다({reasons})."
        every = official_ids + sorted(task.local)
        if not every:
            continue
        group = Cluster(seed_id=min(every), doc_ids=every)
        task_id = _upsert_task(db, Named(task.name, description), group, confidence="medium")
        result.assigned += _attach(db, task_id, official_ids, confidence="high",
                                   evidence=f"{OFFICIAL_EVIDENCE} · {task.basis}", rounds=task.round_of)
        for confidence in ("high", "medium", "low"):
            for evidence in (SAME_EVIDENCE, NUMBER_EVIDENCE, TITLE_EVIDENCE, NEAR_EVIDENCE):
                ids = sorted(doc_id for doc_id, value in task.local.items() if value == (confidence, evidence))
                result.matched_to_official += _attach(db, task_id, ids, confidence=confidence,
                                                      evidence=evidence, rounds=task.round_of)
        _store_reading(db, task_id, every)
        result.named_by_official += 1


def _save_official_only(db: Database, result: Discovery, plan) -> Discovery:
    """의미 색인도 폴더 구조도 없고 공문만 있을 때."""
    _clear_proposals(db)
    db.con.execute("DELETE FROM discovery_holds")
    _save_official(db, result, plan)
    result.assigned = db.con.execute(
        "SELECT COUNT(DISTINCT td.doc_id) FROM task_docs td JOIN tasks t ON t.id=td.task_id "
        "WHERE t.not_a_task=0 AND t.merged_into IS NULL").fetchone()[0]
    result.unassigned = db.unclassified_count()
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
    coherent: bool | None = None


def _name(client: OllamaClient | None, names: list[str], tokens: list[str], evidence=None) -> Named:
    """모델이 없거나 실패하면 파일명 공통 낱말로 대신한다."""
    fallback = _from_tokens(tokens)
    if client is None:
        return Named(fallback)

    naming = (name_task(client, names[:MAX_TITLES_FOR_NAMING], evidence) if evidence else
              name_task(client, names[:MAX_TITLES_FOR_NAMING]))
    if naming.error:
        return Named(fallback, error=naming.error)
    if not naming.coherent:
        # 모델이 하나의 업무로 보지 않았다. 억지로 이름을 붙이지 않는다.
        return Named(fallback, description=naming.description, coherent=False)
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
    항목·단계·수행 기록이 연쇄로 함께 지워진다 — 후임자가 '이 업무가 맞습니다'를
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


def _upsert_task(db: Database, named: Named, group: Cluster, confidence: str | None = None) -> int:
    """Names are labels, not proof of business identity. Keep distinct groups separate."""
    confidence = confidence or group.confidence
    label = named.label
    suffix = 0
    while db.con.execute("SELECT 1 FROM tasks WHERE name=?", (label,)).fetchone():
        suffix += 1
        label = f"{named.label} ({group.seed_id}{'-' + str(suffix) if suffix > 1 else ''})"
    description = named.description or (
        f"파일명과 내용이 비슷한 문서 {group.size}건을 묶었습니다. "
        "이름을 짓지 못해 공통 낱말로 대신했습니다."
    )
    db.con.execute(
        "INSERT INTO tasks(name, description, origin, status, confidence) "
        "VALUES (?, ?, 'ai', 'proposed', ?) ON CONFLICT(name) DO NOTHING",
        (label, description, confidence),
    )
    row = db.con.execute("SELECT id FROM tasks WHERE name = ?", (label,)).fetchone()
    task_id = row["id"]
    # 사람이 고친 업무의 설명은 덮어쓰지 않는다.
    db.con.execute(
        "UPDATE tasks SET description = ? WHERE id = ? AND status = 'proposed'",
        (description, task_id),
    )
    if confidence == "low":
        db.con.execute("UPDATE tasks SET review_state='weak' WHERE id=?", (task_id,))
    return task_id


def _attach(db: Database, task_id: int, doc_ids: list[int], confidence: str = "medium",
            evidence: str | None = None, rounds: dict[int, str] | None = None) -> int:
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
        "INSERT INTO task_docs(task_id, doc_id, origin, confidence, evidence, round_key) "
        "VALUES (?, ?, 'ai', ?, ?, ?) ON CONFLICT(task_id, doc_id) DO NOTHING",
        [(task_id, doc_id, confidence, evidence, (rounds or {}).get(doc_id, ""))
         for doc_id in targets],
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
               d.char_count, d.path,
               (SELECT path FROM sources s WHERE s.id = d.source_id) AS root,
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
            memo=docrole.classify(row["filename"]).role == docrole.MEMO,
            aside=bool(set(docrole.path_modifiers(row["path"] or "", row["root"] or ""))
                       & {folders.OLD, folders.BACKUP, folders.REFERENCE, folders.RECEIVED}),
        )
        for row in rows
    ]
