"""업무 사이의 관계 — 어느 업무가 어느 업무와 엮여 있는가.

전임자의 폴더는 한 가지 일을 여러 곳에 나눠 둔다. 실제 자료에서 우수공무원 선발의 심의
회의는 위원회 폴더에, 소송 지원의 심의는 위원회 제1차 회의에 있었고, 실행계획·위원회·
선발·교육은 모두 종합평가의 평가지표였다. 업무를 합치지는 않는다(전임자의 폴더를
따른다). 엮여 있다는 것만 근거와 함께 알린다.

읽는 근거 셋. 어느 것도 본문의 뜻을 짐작하지 않는다.
  · 폴더 이름의 괄호      '제3회 위원회(상반기 우수공무원 선발)' — 괄호 안에 다른 업무 이름
  · 다른 업무 폴더 아래   '위원회\\소송 지원 관련\\…' — 그 업무의 폴더 안에 놓인 일
  · 같은 문서             한 문서(같은 해시)가 두 업무에 들어 있다
  · 평가지표              평가표의 지표 이름에 이 업무 이름이 들어 있다

모두 '관계기관' 갈래의 "관련 업무 · 이름" 항목으로 올라간다. 근거 상태는 추정이다.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from . import docrole, folders, workstate as ws
from . import reconstruction as rc

RELATED_PREFIX = "관련 업무 · "
_PAREN = re.compile(r"[\(（]([^()（）]{2,40})[\)）]")
# 지표 줄: '01. 적극행정 실행계획 수립', '3열: 06. 적극행정위원회 현안처리 건수'
_INDICATOR = re.compile(r"(?:^|열:\s*|\|\s*)(?P<no>\d{1,2})\.\s*(?P<name>[가-힣][^|\n]{3,40}?)\s*(?=\||$)", re.MULTILINE)
_EVALUATION = re.compile(r"평가")
# 업무 이름에 흔해서 그것만으로는 어느 업무인지 가리지 못하는 말
_GENERIC = frozenset({"관련", "지원", "공무원", "추진", "운영", "자료", "공유", "조사", "현황", "제도",
                      "계획", "결과", "선정", "제출", "활성화", "지자체", "수시"})
_MAX_REASONS = 3
_MIN_INDICATORS = 8     # 번호 붙은 지표가 이만큼은 있어야 평가표로 본다
_COMMON_IN_SHEET = 3    # 지표 이만큼에 나오는 말은 평가표 안에서 흔한 말이다


def _tokens(names: dict[int, str]) -> dict[int, list[str]]:
    """업무마다 그 업무를 가리키는 낱말. 절반 넘는 업무 이름에 나오는 말은 뺀다."""
    words = {task: [w for w in re.findall(r"[가-힣A-Za-z]{2,}", name)] for task, name in names.items()}
    seen = Counter(word for found in words.values() for word in set(found))
    limit = max(1, len(names) // 2)
    out = {}
    for task, found in words.items():
        own = [w for w in found if w not in _GENERIC and (seen[w] <= limit or len(names) <= 2)]
        # 붙여 쓴 이름('적극행정위원회')은 통째로가 그 업무다.
        out[task] = own or ([names[task].replace(" ", "")] if len(found) == 1 else [])
    return out


def _mentions(text: str, tokens: list[str]) -> bool:
    packed = text.replace(" ", "")
    return any(token in packed for token in tokens)


def find(db) -> dict[int, dict[int, list[tuple[str, int | None, str]]]]:
    """업무 → 엮인 업무 → [(까닭, 근거 문서, 근거 글)]. 저장하지 않는다."""
    tasks = db.con.execute(
        "SELECT id, name FROM tasks WHERE COALESCE(not_a_task, 0) = 0 AND merged_into IS NULL").fetchall()
    names = {row["id"]: row["name"] for row in tasks}
    if len(names) < 2:
        return {}
    tokens = _tokens(names)
    docs = db.con.execute(
        "SELECT td.task_id, d.id, d.filename, d.path, d.hash, s.path AS root, r.role "
        "FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "LEFT JOIN sources s ON s.id = d.source_id LEFT JOIN doc_roles r ON r.doc_id = d.id "
        "WHERE d.missing_since IS NULL ORDER BY d.id").fetchall()
    docs = [row for row in docs if row["task_id"] in names]
    out: dict[int, dict[int, list]] = {}

    def link(a: int, b: int, why_a: str, why_b: str, doc_id: int | None, quote: str) -> None:
        for me, other, why in ((a, b, why_a), (b, a, why_b)):
            reasons = out.setdefault(me, {}).setdefault(other, [])
            if why not in [reason[0] for reason in reasons]:
                reasons.append((why, doc_id, quote))

    # 폴더 이름. 업무마다 한 폴더는 한 번만 본다.
    task_folder: dict[int, str] = {}
    seen_folders: set[tuple[int, str]] = set()
    for row in docs:
        parts = folders.relative_parts(row["path"] or "", row["root"] or "")
        owner = row["task_id"]
        for depth, part in enumerate(parts):
            if (owner, part) in seen_folders:
                continue
            seen_folders.add((owner, part))
            label = folders.parse_part(part).label
            for other, found in tokens.items():
                if other == owner or not found:
                    continue
                for inside in _PAREN.findall(part):
                    if _mentions(inside, found):
                        link(owner, other,
                             f"‘{part}’ 폴더가 ‘{names[other]}’의 일을 다룹니다",
                             f"‘{names[owner]}’의 ‘{part}’ 폴더에 이 업무의 자료가 있습니다",
                             row["id"], part)
                # 이 업무의 폴더가 다른 업무 이름의 폴더 아래에 있다
                if depth < len(parts) - 1 and label and _mentions(label, found) \
                        and folders.compact(label) == folders.compact(names[other]):
                    task_folder[owner] = part
                    link(owner, other,
                         f"이 업무의 폴더가 ‘{names[other]}’ 폴더 안에 있습니다",
                         f"이 폴더 안에 ‘{names[owner]}’의 자료가 따로 있습니다",
                         row["id"], part)

    # 같은 문서
    by_hash: dict[str, list] = {}
    for row in docs:
        if row["hash"]:
            by_hash.setdefault(row["hash"], []).append(row)
    for same in by_hash.values():
        owners = {row["task_id"] for row in same}
        if len(owners) != 2:
            continue        # 셋 이상에 퍼진 문서(공통 서식·지침)는 관계를 말해 주지 않는다
        a, b = sorted(owners)
        name = same[0]["filename"]
        link(a, b, f"같은 문서가 두 업무에 있습니다: {name}", f"같은 문서가 두 업무에 있습니다: {name}",
             same[0]["id"], name)

    # 평가지표. 평가표·평가 안내는 이름에 '평가'가 있고 번호 붙은 지표가 여럿 적혀 있다.
    # 참고 자료 묶음은 누구의 실적도 아니다.
    from . import bundles

    not_work = {task for task in names if bundles.kind(db, task)}
    graded: set[tuple[int, int]] = set()
    for row in docs:
        if not _EVALUATION.search(row["filename"] or "") or not _EVALUATION.search(names[row["task_id"]]):
            continue
        body = db.con.execute("SELECT body FROM document_index WHERE doc_id = ?", (row["id"],)).fetchone()
        matches = list(_INDICATOR.finditer(body["body"] if body else ""))
        if len({match.group("no") for match in matches}) < _MIN_INDICATORS:
            continue
        indicators: dict[str, str] = {}
        for match in matches:
            indicators.setdefault(match.group("no"), match.group("name").strip())
        for other, found in tokens.items():
            if other == row["task_id"] or other in not_work or not found or (other, row["task_id"]) in graded:
                continue
            # 지표 여럿에 나오는 말('우수사례')은 그 업무를 가리키지 못한다. 한두 지표에만
            # 나오는 말로만 잇는다.
            hits = [(no, name) for no, name in indicators.items()
                    if _mentions(name, [token for token in found
                                        if sum(_mentions(n, [token]) for n in indicators.values())
                                        < _COMMON_IN_SHEET])]
            if not hits:
                continue
            # 해마다의 평가표가 같은 지표를 번호만 달리 싣는다. 업무 한 쌍에 한 번만 잇는다.
            graded.add((other, row["task_id"]))
            indicator = f"{hits[0][0]}. {hits[0][1]}"
            link(other, row["task_id"],
                 f"평가지표 ‘{indicator}’의 실적입니다. 증빙을 모아 두세요",
                 f"지표 ‘{indicator}’는 ‘{names[other]}’의 실적으로 냅니다",
                 row["id"], indicator)
    return out


def items(db, task_id: int, found: dict | None = None, basis: date | None = None) -> list[dict]:
    """업무 하나의 관련 업무를 항목 후보로 돌려준다."""
    found = find(db) if found is None else found
    names = {row["id"]: row["name"] for row in db.con.execute("SELECT id, name FROM tasks")}
    out = []
    for other, reasons in sorted(found.get(task_id, {}).items(), key=lambda pair: names.get(pair[0], "")):
        shown = reasons[:_MAX_REASONS]
        content = " / ".join(why for why, _doc, _quote in shown)
        if len(reasons) > _MAX_REASONS:
            content += f" 외 {len(reasons) - _MAX_REASONS}가지"
        out.append({
            "kind": rc.AGENCY, "slot": RELATED_PREFIX + names.get(other, str(other)),
            "content": content[:400], "basis_state": ws.INFERRED, "applicability": ws.UNKNOWN,
            "evidence": [(doc, "", quote) for _why, doc, quote in shown if doc],
        })
    return out
