"""빠른 개괄 — 자료 지도 (Survey).

파싱·임베딩 전에 파일 시스템 메타데이터만으로 폴더 구조, 자료량,
연도 분포, 꼬리표, 중복 해시를 빠르게 파악한다.

원칙:
  1. 원본 본체나 파싱 결과를 읽지 않는다. DB의 메타데이터(path, size, mtime, hash)만 쓴다.
  2. 1,000개 파일도 수초 이내에 트리와 비중 점수를 계산한다.
  3. 계산된 비중(weight)은 S5의 업무 분석 순서와 S6의 업무 카드 정렬에 쓰인다.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath, PurePosixPath

from ..db import Database
from .clustering import _tokenize
from .folders import parse_part


@dataclass
class FolderSurvey:
    path: str                   # 상대 경로 ("." 또는 "기획/2023")
    name: str                   # 폴더 이름
    depth: int                  # 0: 루트, 1: 최상위 폴더...
    file_count: int = 0         # 전체 파일 수
    doc_count: int = 0          # 분석 대상 문서 수
    non_doc_count: int = 0      # 비문서(그림, 실행파일 등) 수
    total_size: int = 0         # 총 용량(바이트)
    years: dict[str, int] = field(default_factory=dict)  # {"2023": 12, "2024": 5}
    latest_mtime: str = ""      # ISO 날짜 YYYY-MM-DD
    tokens: list[tuple[str, int]] = field(default_factory=list)  # [(토큰, 빈도)]
    round_tags: list[str] = field(default_factory=list)  # ["2023", "제1차"]
    weight: float = 0.0         # 자료량 비중 점수 (0.0 ~ 1.0)
    children: list["FolderSurvey"] = field(default_factory=list)


@dataclass
class SurveyResult:
    total_files: int = 0
    total_docs: int = 0
    total_non_docs: int = 0
    total_size: int = 0
    duplicate_hash_ratio: float = 0.0
    non_doc_ratio: float = 0.0
    year_range: list[int] = field(default_factory=list)  # [min_year, max_year]
    root_folders: list[FolderSurvey] = field(default_factory=list)
    folder_weights: dict[str, float] = field(default_factory=dict)  # {상대폴더경로: 점수}
    built_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SurveyResult":
        def _parse_folder(d: dict) -> FolderSurvey:
            children = [_parse_folder(c) for c in d.get("children", [])]
            copied = dict(d)
            copied["children"] = children
            return FolderSurvey(**copied)

        root_folders = [_parse_folder(rf) for rf in data.get("root_folders", [])]
        return cls(
            total_files=data.get("total_files", 0),
            total_docs=data.get("total_docs", 0),
            total_non_docs=data.get("total_non_docs", 0),
            total_size=data.get("total_size", 0),
            duplicate_hash_ratio=data.get("duplicate_hash_ratio", 0.0),
            non_doc_ratio=data.get("non_doc_ratio", 0.0),
            year_range=data.get("year_range", []),
            root_folders=root_folders,
            folder_weights=data.get("folder_weights", {}),
            built_at=data.get("built_at", ""),
        )


def _normalize_relpath(path_str: str) -> str:
    """윈도우/POSIX 경로 구분자를 통일하고 표준 상대경로를 반환한다."""
    cleaned = path_str.replace("\\", "/").strip("/")
    return cleaned if cleaned else "."


def _extract_year(mtime_str: str | None, ctime_str: str | None) -> str | None:
    for s in (mtime_str, ctime_str):
        if s and len(s) >= 4 and s[:4].isdigit():
            year_num = int(s[:4])
            if 1980 <= year_num <= 2040:
                return str(year_num)
    return None


def build(db: Database) -> SurveyResult:
    """DB에 등록된 문서 메타데이터를 기반으로 자료 지도를 구축한다."""
    sources = db.work_sources()
    source_map: dict[int, str] = {s["id"]: s["path"] for s in sources}

    # 활성 문서 조회
    rows = db.con.execute(
        "SELECT id, source_id, path, filename, ext, size, fs_mtime, fs_ctime, hash, parse_status "
        "FROM documents "
        "WHERE missing_since IS NULL"
    ).fetchall()

    if not rows:
        result = SurveyResult(built_at=datetime.now().isoformat())
        _save_result(db, result)
        return result

    total_files = len(rows)
    total_docs = 0
    total_non_docs = 0
    total_size = 0
    hashes: list[str] = []
    years_set: set[int] = set()

    # 폴더별 집계 구조
    # direct_files[folder_relpath] = list of doc rows
    direct_files: dict[str, list] = defaultdict(list)

    for row in rows:
        is_doc = (row["parse_status"] != "skipped")
        if is_doc:
            total_docs += 1
        else:
            total_non_docs += 1

        total_size += row["size"] or 0
        if row["hash"]:
            hashes.append(row["hash"])

        yr = _extract_year(row["fs_mtime"], row["fs_ctime"])
        if yr:
            years_set.add(int(yr))

        # 소스 루트 대비 상대 경로 계산
        src_root = source_map.get(row["source_id"], "")
        doc_path = row["path"]

        rel_dir = "."
        if src_root:
            try:
                p_doc = Path(doc_path)
                p_root = Path(src_root)
                rel = p_doc.parent.relative_to(p_root)
                rel_dir = _normalize_relpath(str(rel))
            except Exception:
                # relative_to 실패 시 문자열 비교
                norm_doc = doc_path.replace("\\", "/")
                norm_root = src_root.replace("\\", "/")
                if norm_doc.startswith(norm_root):
                    sub = norm_doc[len(norm_root):].lstrip("/").rsplit("/", 1)
                    rel_dir = _normalize_relpath(sub[0] if len(sub) == 2 else ".")
        else:
            p_doc = Path(doc_path)
            rel_dir = _normalize_relpath(p_doc.parent.name)

        direct_files[rel_dir].append(row)

    # 중복 해시 비율
    dup_ratio = 0.0
    if hashes:
        unique_hashes = len(set(hashes))
        dup_ratio = round((len(hashes) - unique_hashes) / len(hashes), 4)

    non_doc_ratio = round(total_non_docs / total_files, 4) if total_files > 0 else 0.0

    # 폴더 트리 구성
    # 알려진 모든 폴더(루트 및 부모 폴더 포함) 수집
    all_dirs: set[str] = {"."}
    for d in direct_files:
        all_dirs.add(d)
        if d != ".":
            parts = d.split("/")
            for i in range(1, len(parts)):
                all_dirs.add("/".join(parts[:i]))

    # 폴더별 재귀적 집계 (자신 + 하위 전체)
    folder_stats: dict[str, dict] = {}
    for d in all_dirs:
        folder_stats[d] = {
            "file_count": 0,
            "doc_count": 0,
            "non_doc_count": 0,
            "total_size": 0,
            "years": Counter(),
            "latest_mtime": "",
            "tokens": Counter(),
            "round_tags": set(),
        }

    for d, file_list in direct_files.items():
        # 해당 폴더 및 모든 조상 폴더에 누적
        ancestors = ["."]
        if d != ".":
            parts = d.split("/")
            for i in range(1, len(parts) + 1):
                ancestors.append("/".join(parts[:i]))

        for row in file_list:
            is_doc = (row["parse_status"] != "skipped")
            size = row["size"] or 0
            mtime = row["fs_mtime"] or ""
            yr = _extract_year(row["fs_mtime"], row["fs_ctime"])
            fn_tokens = _tokenize(row["filename"])

            for anc in ancestors:
                st = folder_stats[anc]
                st["file_count"] += 1
                if is_doc:
                    st["doc_count"] += 1
                else:
                    st["non_doc_count"] += 1
                st["total_size"] += size
                if yr:
                    st["years"][yr] += 1
                if mtime and mtime > st["latest_mtime"]:
                    st["latest_mtime"] = mtime
                for tok in fn_tokens:
                    st["tokens"][tok] += 1

        # 폴더명 자체에서도 토큰 및 round_tag 추출
        if d != ".":
            for part in d.split("/"):
                parsed = parse_part(part)
                if parsed.round_key:
                    for anc in ancestors:
                        folder_stats[anc]["round_tags"].add(parsed.round_key)
                if parsed.label:
                    for tok in _tokenize(parsed.label):
                        for anc in ancestors:
                            folder_stats[anc]["tokens"][tok] += 1

    # 상위 후보 폴더(1레벨 폴더) 대상 비중(weight) 계산
    # 만약 루트에만 파일이 있다면 루트(".")가 대상
    candidate_folders = [d for d in all_dirs if d != "." and "/" not in d]
    if not candidate_folders and "." in all_dirs:
        candidate_folders = ["."]

    folder_weights: dict[str, float] = {}
    if candidate_folders:
        max_docs = max((folder_stats[d]["doc_count"] for d in candidate_folders), default=1) or 1
        max_years = max((len(folder_stats[d]["years"]) for d in candidate_folders), default=1) or 1

        for d in candidate_folders:
            st = folder_stats[d]
            norm_docs = st["doc_count"] / max_docs
            norm_years = len(st["years"]) / max_years
            # 문서 수 70%, 연도 분포 다양성 30% 반영
            score = round(0.7 * norm_docs + 0.3 * norm_years, 3)
            folder_weights[d] = score
    else:
        folder_weights["."] = 1.0

    # 트리 계층 생성
    def _build_tree(cur_path: str, depth: int) -> FolderSurvey:
        name = "(전체)" if cur_path == "." else cur_path.rsplit("/", 1)[-1]
        st = folder_stats[cur_path]
        top_tokens = st["tokens"].most_common(10)
        round_tags = sorted(st["round_tags"])
        weight = folder_weights.get(cur_path, 0.0)

        # 직계 자식 폴더 찾기
        children_paths = []
        if cur_path == ".":
            children_paths = sorted([d for d in all_dirs if d != "." and "/" not in d])
        else:
            prefix = cur_path + "/"
            prefix_len = len(cur_path.split("/"))
            children_paths = sorted([
                d for d in all_dirs
                if d.startswith(prefix) and len(d.split("/")) == prefix_len + 1
            ])

        children = [_build_tree(cp, depth + 1) for cp in children_paths]

        return FolderSurvey(
            path=cur_path,
            name=name,
            depth=depth,
            file_count=st["file_count"],
            doc_count=st["doc_count"],
            non_doc_count=st["non_doc_count"],
            total_size=st["total_size"],
            years=dict(st["years"]),
            latest_mtime=st["latest_mtime"],
            tokens=top_tokens,
            round_tags=round_tags,
            weight=weight,
            children=children,
        )

    root_node = _build_tree(".", 0) if "." in all_dirs else None
    root_folders = [root_node] if root_node else []

    year_range = [min(years_set), max(years_set)] if years_set else []

    result = SurveyResult(
        total_files=total_files,
        total_docs=total_docs,
        total_non_docs=total_non_docs,
        total_size=total_size,
        duplicate_hash_ratio=dup_ratio,
        non_doc_ratio=non_doc_ratio,
        year_range=year_range,
        root_folders=root_folders,
        folder_weights=folder_weights,
        built_at=datetime.now().isoformat(),
    )

    _save_result(db, result)
    return result


def _save_result(db: Database, result: SurveyResult) -> None:
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    db.set_meta("survey_result", payload)
    db.set_meta("survey_built_at", result.built_at)


def get_survey(db: Database) -> SurveyResult | None:
    raw = db.get_meta("survey_result")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return SurveyResult.from_dict(data)
    except Exception:
        return None


def get_folder_weights(db: Database) -> dict[str, float]:
    survey = get_survey(db)
    if not survey:
        return {}
    return survey.folder_weights
