"""과부하 합성 자료 생성기 시험.

가장 작은 프로필(smoke)로 모든 종류의 파일을 하나씩 만들고, 정답 원장이 말한 대로
읽히는지 본다 — 손상·암호·구형 형식이 조용히 '정상'이 되거나, 멀쩡한 문서가
실패로 떨어지면 여기서 잡힌다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ingest import scanner
from app.ingest.parsers import parse
from app.tools import make_stress_fixtures as stress


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("stress") / "자료"
    assert stress.main([str(out), "--profile", "smoke"]) == 0
    manifest = json.loads(out.with_name("자료_manifest.json").read_text(encoding="utf-8"))
    return out, manifest


def test_manifest_lists_exactly_the_files_on_disk(corpus):
    out, manifest = corpus
    on_disk = {path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file()}
    assert on_disk == {entry["path"] for entry in manifest["entries"]}
    assert manifest["files"] == len(on_disk)


def test_every_document_parses_as_the_manifest_says(corpus):
    out, manifest = corpus
    wrong = []
    for entry in manifest["entries"]:
        if not entry["document"] or entry["kind"] == "excluded":
            continue
        status = parse(out / entry["path"]).status
        if status not in entry["expect"]:
            wrong.append((entry["path"], status, entry["expect"]))
    assert not wrong


def test_hard_cases_are_all_present(corpus):
    _out, manifest = corpus
    expected = {status for entry in manifest["entries"] for status in (entry["expect"] or [])}
    assert {"ok", "partial", "empty", "failed", "encrypted", "unsupported"} <= expected
    kinds = set(manifest["by_kind"])
    assert {"step", "version", "medium", "large", "edge", "excluded", "non_document"} <= kinds
    assert len(manifest["tasks"]) >= len(stress.TASKS)


def test_scanner_skips_what_the_manifest_marks_excluded(corpus):
    out, manifest = corpus
    walked = {path.relative_to(out).as_posix() for path, _stat, _error in scanner._walk(out, [])}
    excluded = {entry["path"] for entry in manifest["entries"] if entry["kind"] == "excluded"}
    assert excluded and not (walked & excluded)
    assert walked == {entry["path"] for entry in manifest["entries"]} - excluded


def test_same_seed_gives_the_same_corpus(corpus, tmp_path):
    _out, manifest = corpus
    again = tmp_path / "자료"
    assert stress.main([str(again), "--profile", "smoke"]) == 0
    second = json.loads(again.with_name("자료_manifest.json").read_text(encoding="utf-8"))
    assert [e["path"] for e in second["entries"]] == [e["path"] for e in manifest["entries"]]
    assert second["chars"] == manifest["chars"]


def test_paths_stay_inside_the_windows_limit(corpus):
    _out, manifest = corpus
    assert max(len(entry["path"]) for entry in manifest["entries"]) <= stress.MAX_REL_PATH


def test_manifest_must_live_outside_the_output_folder(tmp_path):
    out = tmp_path / "자료"
    assert stress.main([str(out), "--profile", "smoke", "--manifest", str(out / "정답.json")]) == 2
