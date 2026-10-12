"""전임자의 폴더 구조를 읽는다 — 업무·회차·단계·자료 성격의 1차 단서.

전임자는 대개 일을 폴더로 정리해 둔다. 맨 위 폴더가 업무이고, 그 아래에
`1. 선발계획 → 2. 부서제출 → … → 9. 시상식`처럼 순서를, `[2023-06-23] 결과 보고`처럼
기안일을, `old`·`bak`·`ref`·`부서제출`처럼 자료의 성격을 적어 둔다. 본문이 닮았는지만
보고 묶으면 이 정보를 전부 버리게 된다. 실제 자료에서 한 업무(우수공무원 선발)의
문서가 계획서·추천서·견적서·시나리오로 서로 닮지 않아 79개 업무로 흩어졌다.

이 모듈은 이름을 읽기만 한다. 규칙이고, 모델을 쓰지 않는다. 폴더가 평평하거나
이름에 뜻이 없으면 아무것도 주장하지 않는다 — 그때는 부르는 쪽이 본문 유사도로 묶는다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

OLD, BACKUP, REFERENCE, RECEIVED, SUBMITTED, MISC = (
    "old", "backup", "reference", "received", "submitted", "misc")
MODIFIER_LABEL = {
    OLD: "이전 자료", BACKUP: "이전 판", REFERENCE: "참고", RECEIVED: "받은 자료",
    SUBMITTED: "제출본", MISC: "기타",
}

_ORDER = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*(?!\d)")
_DATE = re.compile(r"\[?\s*((?:19|20)\d{2})[-.](\d{1,2})[-.](\d{1,2})\s*\]?")
_PAREN = re.compile(r"\([^()]*\)|（[^（）]*）")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*(?:년도|년)?(?!\d)")
_HALF = re.compile(r"(상|하)반기")
_SESSION = re.compile(r"제\s*(\d{1,3})\s*(?:회|차)")
_BRACKETS = str.maketrans("", "", "[]【】「」『』〈〉<>")

_MODIFIERS: tuple[tuple[str, re.Pattern], ...] = (
    (BACKUP, re.compile(r"^(?:bak|backup|백업)$", re.I)),
    (OLD, re.compile(r"^(?:old|이전|구버전|과거|예전|지난\s*자료)$|[_\s]old$", re.I)),
    (REFERENCE, re.compile(r"^(?:ref|reference|참고|참고\s*자료|참조|참조\s*자료)$", re.I)),
    (RECEIVED, re.compile(r"^(?:source|취합)$|소스$|^부서\s*(?:별\s*)?(?:제출|추천|취합)", re.I)),
    (SUBMITTED, re.compile(r"^(?:제출|제출본|제출\s*자료)$")),
    (MISC, re.compile(r"^(?:기타|etc|misc|temp|tmp|임시|새\s*폴더(?:\s*\d+)?)$", re.I)),
)
# 업무 폴더가 될 수 없는 성격. 참고·받은 자료·제출본 폴더는 그 위 업무에 딸린 것이다.
_NOT_A_TASK = {OLD, BACKUP, REFERENCE, RECEIVED, SUBMITTED, MISC}


@dataclass(frozen=True, slots=True)
class Part:
    """폴더 이름 하나를 읽은 결과."""

    raw: str
    label: str = ""              # 번호·날짜·연도·괄호를 뗀 이름
    order: int | None = None     # "3. …"의 3
    date: str | None = None      # "[2023-06-23] …"의 날짜
    year: int | None = None
    half: str | None = None      # 상 | 하
    session: int | None = None   # 제N회·제N차
    modifier: str | None = None

    @property
    def transparent(self) -> bool:
        """업무 이름이 될 수 없어 건너뛰는 폴더(연도만, old 묶음, 기타)."""
        return not self.label or self.modifier in (OLD, BACKUP, MISC)

    @property
    def is_step(self) -> bool:
        return self.order is not None or self.date is not None

    @property
    def round_key(self) -> str:
        """회차를 가리키는 말. 없으면 빈 문자열."""
        parts = []
        if self.year:
            parts.append(str(self.year))
        if self.half:
            parts.append(f"{self.half}반기")
        if self.session:
            parts.append(f"제{self.session}차")
        return " ".join(parts)


def parse_part(name: str) -> Part:
    text = name.translate(_BRACKETS).strip()
    order = date = year = half = session = None

    found = _ORDER.match(text)
    if found:
        order = int(found.group(1))
        text = text[found.end():]
    found = _DATE.search(text)
    if found:
        y, m, d = (int(g) for g in found.groups())
        if 1 <= m <= 12 and 1 <= d <= 31:
            date = f"{y:04d}-{m:02d}-{d:02d}"
            year = y
            text = text[:found.start()] + " " + text[found.end():]
    # "13. [2023-11-14] …"처럼 번호 뒤에 날짜가 온 뒤 다시 번호가 붙지는 않는다.
    text = _PAREN.sub(" ", text)
    found = _YEAR.search(text)
    if found:
        year = year or int(re.sub(r"\D", "", found.group(0)))
        text = _YEAR.sub(" ", text)
    found = _HALF.search(text)
    if found:
        half = found.group(1)
        text = _HALF.sub(" ", text)
    found = _SESSION.search(text)
    if found:
        session = int(found.group(1))
        text = _SESSION.sub(" ", text)
    label = " ".join(text.replace("_", " ").split()).strip(" -·,&")
    modifier = next((kind for kind, pattern in _MODIFIERS if pattern.search(label)), None)
    return Part(name, label, order, date, year, half, session, modifier)


def compact(label: str) -> str:
    return re.sub(r"[\s·,&/\-]+", "", label)


def relative_parts(path: str, root: str) -> list[str]:
    """자료원 아래의 폴더 이름들(파일 이름 제외). 자료원 밖이면 빈 목록."""
    target, base = PureWindowsPath(path), PureWindowsPath(root)
    try:
        return list(target.relative_to(base).parent.parts)
    except ValueError:
        return []


@dataclass(slots=True)
class Placement:
    """문서 하나가 폴더 구조에서 놓인 자리."""

    task_parts: tuple[str, ...] = ()     # 업무 폴더까지의 경로(자료원 기준). 없으면 낱장
    label: str = ""                      # 업무 폴더의 이름
    round_key: str = ""
    step: Part | None = None             # 가장 가까운 번호·날짜 폴더
    modifiers: tuple[str, ...] = ()      # 업무 폴더 아래에서 만난 성격(old, reference, …)

    @property
    def loose(self) -> bool:
        return not self.task_parts


@dataclass(slots=True)
class Tree:
    """자료원 하나의 폴더 구조. 문서 경로들로 만든다."""

    root: str
    children: dict[tuple[str, ...], set[str]] = field(default_factory=dict)
    _parts: dict[str, Part] = field(default_factory=dict)

    @classmethod
    def build(cls, root: str, paths) -> "Tree":
        tree = cls(root)
        for path in paths:
            parts = relative_parts(path, root)
            for depth in range(len(parts)):
                tree.children.setdefault(tuple(parts[:depth]), set()).add(parts[depth])
        return tree

    def part(self, name: str) -> Part:
        found = self._parts.get(name)
        if found is None:
            found = self._parts[name] = parse_part(name)
        return found

    def has_workflow(self, folder: tuple[str, ...]) -> bool:
        """이 폴더 바로 아래에 번호·날짜가 붙은 단계 폴더가 둘 이상 있는가."""
        return sum(self.part(child).is_step for child in self.children.get(folder, ())) >= 2

    def place(self, path: str) -> Placement:
        parts = relative_parts(path, self.root)
        task_at = None
        for depth, name in enumerate(parts):
            part = self.part(name)
            if part.transparent or part.modifier in _NOT_A_TASK:
                continue
            task_at = depth
            break
        if task_at is None:
            return Placement(modifiers=self._modifiers(parts))

        # 업무 폴더 안에 이름이 다른 폴더가 자기 단계 구조를 갖고 있으면 그것이 따로
        # 하나의 일이다(위원회 폴더 안의 '소송 지원' — 1.신청 … 5.지급).
        parent = compact(self.part(parts[task_at]).label)
        for depth in range(task_at + 1, len(parts)):
            part = self.part(parts[depth])
            if part.modifier in (REFERENCE, RECEIVED, SUBMITTED):
                break       # 참고·받은 자료 안의 번호 폴더는 남이 정리한 것이다
            if part.transparent or part.is_step:
                continue
            own = compact(part.label)
            related = parent in own or own in parent
            if not related and self.has_workflow(tuple(parts[:depth + 1])):
                task_at, parent = depth, own
        below = parts[task_at + 1:]
        task = self.part(parts[task_at])
        step = next((self.part(name) for name in reversed(below) if self.part(name).is_step), None)
        # 회차는 가장 깊은 폴더가 말하는 것을 쓴다('2024년 …\하반기' → 2024 하반기).
        chain = [self.part(name) for name in reversed(below)] + [task]
        year = next((p.year for p in chain if p.year), None)
        half = next((p.half for p in chain if p.half), None)
        session = next((p.session for p in chain if p.session), None)
        return Placement(
            task_parts=tuple(parts[:task_at + 1]),
            label=task.label,
            round_key=Part("", year=year, half=half, session=session).round_key,
            step=step,
            modifiers=self._modifiers(parts[:task_at]) + self._modifiers(below),
        )

    def _modifiers(self, names) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            self.part(name).modifier for name in names if self.part(name).modifier))


def common_tokens(labels: list[str], min_share: float = 0.5, min_labels: int = 4) -> list[str]:
    """업무 폴더 이름 대부분에 들어 있는 말 — 자료 전체의 주제어라 업무를 가르지 못한다.

    '적극행정 실행계획', '적극행정 중점과제', '적극행정위원회'에서 '적극행정'이 그렇다.
    띄어쓰기가 제각각이라 낱말이 아니라 글자열로 센다.
    """
    distinct = sorted({compact(label) for label in labels if label})
    if len(distinct) < min_labels:
        return []
    tokens = {t for label in labels for t in re.split(r"[\s·,&/\-]+", label) if len(t) >= 2}
    floor = max(2, -(-len(distinct) * min_share // 1))
    counts = Counter({token: sum(token in text for text in distinct) for token in tokens})
    return sorted((t for t, n in counts.items() if n >= floor), key=len, reverse=True)


def core(label: str, common: list[str]) -> str:
    """주제어를 뺀 업무 이름의 알맹이. 다 빠지면 원래 이름을 쓴다."""
    text = compact(label)
    for token in common:
        stripped = text.replace(token, "")
        if stripped:
            text = stripped
    return text
