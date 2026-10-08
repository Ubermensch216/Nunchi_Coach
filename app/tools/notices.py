"""오픈소스 고지문 만들기 — 실행물에 든 것과 만드는 데 쓴 것을 모두 밝힌다.

    python -m app.tools.notices [--out build/licenses] [--strict]

SBOM(app.tools.sbom)은 기계가 읽는 목록이고, 이것은 사람이 읽는 고지문이다.
구성요소마다 이름·판·라이선스·출처를 적고, 배포판이 함께 준 라이선스 본문을
그대로 붙인다. 실행물에 넣어 설정 화면과 `NunchiCoach.exe licenses`로 보인다.

방침 (2026-10-06)
  · PyMuPDF는 바꾸지 않고 AGPL-3.0 조건으로 쓴다. 상용 라이선스는 쓰지 않는다.
  · 실행물에 든 것뿐 아니라 빌드·시험 도구까지 **전부** 적는다. 무엇이 실행물에
    들어가고 무엇이 도구인지는 구분해 적는다.

지키는 것
  · 라이선스 이름은 배포판이 스스로 밝힌 표기를 옮긴다. 추측해 채우지 않는다.
  · 본문을 찾지 못한 라이선스는 못 찾았다고 적는다(`missing_texts`). 있는 것처럼
    넘어가지 않는다.
  · 법적 검토가 아니다. 고지문에도 그렇게 적는다.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from .. import __version__
from .lockfile import SKIP, normalize
from .sbom import _license

FILENAME = "THIRD_PARTY_NOTICES.txt"
DEFAULT_OUT = Path("build") / "licenses"
# 이 프로그램의 대응 소스를 받는 곳(AGPL-3.0 §6). 저장소를 옮기면 여기를 고친다.
SOURCE_URL = "https://github.com/Ubermensch216/Nunchi_Coach"

_APP_DIR = Path(__file__).resolve().parents[1]
BUNDLED = _APP_DIR / "licenses" / FILENAME        # 실행물 안에서의 자리
TEXTS_DIR = _APP_DIR / "licenses" / "texts"       # 배포판이 본문을 주지 않는 라이선스
_REQUIREMENTS = _APP_DIR.parent / "requirements.txt"
_DEV_MARK = "개발·시험용"
_TEXT_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")
_MAX_TEXT = 400_000

RUNTIME = "실행물에 포함"
TOOLING = "빌드·시험 도구(실행물에 넣지 않음)"

# 배포판이 본문을 싣지 않아 따로 갖춰야 하는 라이선스: 파일 이름 → 받는 곳
CANONICAL = {
    "AGPL-3.0.txt": "https://www.gnu.org/licenses/agpl-3.0.txt",
    "LGPL-3.0.txt": "https://www.gnu.org/licenses/lgpl-3.0.txt",
    "GPL-3.0.txt": "https://www.gnu.org/licenses/gpl-3.0.txt",
}

# 함께 싣는 글꼴(app/ui/assets/fonts). 파이썬 배포판이 아니라 아래 목록에는
# 잡히지 않으므로 여기에 판과 출처를 적는다. 글꼴 파일을 바꾸면 함께 고친다.
# 라이선스 본문은 TEXTS_DIR의 OFL-1.1-Pretendard.txt(받은 그대로)다.
PRETENDARD_VERSION = "1.3.9"
PRETENDARD_URL = "https://github.com/orioncactus/pretendard"

# 조건이 따르는 구성요소. 키는 정규화한 배포판 이름.
_SOURCES = {
    "pymupdf": "https://github.com/pymupdf/PyMuPDF",
    "pyside6": "https://code.qt.io/cgit/pyside/pyside-setup.git",
    "pyside6-essentials": "https://code.qt.io/cgit/pyside/pyside-setup.git",
    "pyside6-addons": "https://code.qt.io/cgit/pyside/pyside-setup.git",
    "shiboken6": "https://code.qt.io/cgit/pyside/pyside-setup.git",
    "pyinstaller": "https://github.com/pyinstaller/pyinstaller",
}


@dataclass(slots=True)
class Component:
    name: str
    version: str
    license: str
    url: str
    role: str
    texts: list[tuple[str, str]] = field(default_factory=list)


def components() -> list[Component]:
    """설치된 배포판 전부. SBOM과 같은 집합·같은 순서다."""
    runtime = _runtime_names()
    out: dict[str, Component] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        key = normalize(name or "")
        if not name or key in SKIP or key in out:
            continue
        out[key] = Component(
            name, dist.version, _license(dist) or "표기 없음",
            _SOURCES.get(key) or _url(dist),
            RUNTIME if key in runtime else TOOLING, _texts(dist))
    return [out[key] for key in sorted(out)]


def _url(dist) -> str:
    home = (dist.metadata.get("Home-page") or "").strip()
    if home:
        return home
    urls = {}
    for value in dist.metadata.get_all("Project-URL") or []:
        label, _, url = value.partition(",")
        urls[label.strip().lower()] = url.strip()
    for label in ("homepage", "source", "sources", "repository", "documentation"):
        if label in urls:
            return urls[label]
    return next(iter(urls.values()), f"https://pypi.org/project/{dist.metadata['Name']}/")


def _texts(dist) -> list[tuple[str, str]]:
    """배포판이 함께 준 라이선스·고지 파일. 준 그대로 싣는다."""
    found = []
    for item in dist.files or []:
        if ".dist-info" not in item.parts[0] or not item.name.upper().startswith(_TEXT_NAMES):
            continue
        try:
            text = Path(dist.locate_file(item)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.append(("/".join(item.parts[1:]), text[:_MAX_TEXT].strip()))
    return sorted(found)


def _runtime_names() -> set[str]:
    """실행 의존성(requirements.txt의 개발·시험용 표시 앞)과 그것이 끌어오는 것."""
    roots = []
    try:
        lines = _REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if _DEV_MARK in line:
            break
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match and not line.lstrip().startswith("#"):
            roots.append(normalize(match.group(1)))

    seen: set[str] = set()
    while roots:
        key = roots.pop()
        if key in seen:
            continue
        seen.add(key)
        try:
            requires = metadata.requires(key) or []
        except metadata.PackageNotFoundError:
            continue
        for requirement in requires:
            if "extra ==" in requirement or "extra==" in requirement:
                continue
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
            if match:
                roots.append(normalize(match.group(1)))
    return seen


def canonical_texts() -> list[tuple[str, str]]:
    if not TEXTS_DIR.is_dir():
        return []
    return [(path.name, path.read_text(encoding="utf-8", errors="replace").strip())
            for path in sorted(TEXTS_DIR.glob("*.txt"))]


def missing_texts() -> list[str]:
    """본문을 갖추지 못한 라이선스. 비어 있어야 고지문이 온전하다."""
    have = {name for name, _text in canonical_texts()}
    return [name for name in CANONICAL if name not in have]


def _python_license() -> str:
    for root in (sys.base_prefix, sys.prefix):
        path = Path(root) / "LICENSE.txt"
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")[:_MAX_TEXT].strip()
    return ""


def render(items: list[Component] | None = None) -> str:
    items = components() if items is None else items
    versions = {normalize(item.name): item.version for item in items}
    python = ".".join(str(part) for part in sys.version_info[:3])
    rule = "=" * 72
    out = [
        f"눈치코치(NunchiCoach) {__version__} — 오픈소스 구성요소 고지",
        rule, "",
        "이 프로그램은 아래 오픈소스 구성요소를 포함하거나, 그것을 써서 만들었습니다.",
        "각 구성요소의 저작권은 해당 저작권자에게 있으며 각자의 라이선스를 따릅니다.",
        "라이선스 이름은 구성요소가 스스로 밝힌 표기를 옮긴 것이고, 이 문서는 법률",
        "검토가 아닙니다.",
        "",
        "1. 조건이 따르는 구성요소",
        "-" * 72,
        "",
        f"■ PyMuPDF {versions.get('pymupdf', '')} 와 그 안의 MuPDF — GNU AGPL-3.0",
        "  PDF 문서를 읽는 데 씁니다. 고치지 않고 그대로 포함합니다. Artifex의 상용",
        "  라이선스는 쓰지 않으며 GNU Affero General Public License 3.0 조건으로",
        "  씁니다. 따라서 이 실행물을 받은 사람은 AGPL-3.0에 따라 이 프로그램의",
        "  대응 소스를 받아 쓰고, 고치고, 같은 조건으로 다시 배포할 수 있습니다.",
        f"    이 프로그램의 소스: {SOURCE_URL}",
        "    PyMuPDF 소스:       https://github.com/pymupdf/PyMuPDF",
        "    MuPDF 소스:         https://github.com/ArtifexSoftware/mupdf",
        "",
        f"■ PySide6 · shiboken6 {versions.get('pyside6', '')} 와 Qt 6 — GNU LGPL-3.0",
        "  화면을 그리는 데 씁니다. 고치지 않았고 동적 라이브러리로 따로 들어 있습니다",
        "  (실행물 폴더의 _internal/PySide6, _internal/shiboken6). 같은 인터페이스의",
        "  다른 판으로 바꿔 넣을 수 있으며, 그렇게 고쳐 쓰기 위한 분석을 막지 않습니다.",
        "    소스: https://download.qt.io/official_releases/QtForPython/",
        "    Qt가 포함하는 제3자 구성요소: https://doc.qt.io/qt-6/licenses-used-in-qt.html",
        "",
        f"■ PyInstaller {versions.get('pyinstaller', '')} — GPL-2.0-or-later, 부트로더 예외",
        "  실행 파일로 묶는 데 쓴 도구입니다. 실행물에는 부트로더만 들어가며, 부트로더",
        "  예외 조항에 따라 묶인 프로그램에 GPL이 미치지 않습니다.",
        "",
        f"■ Python {python} — PSF License 2.0",
        "  실행물에 인터프리터와 표준 라이브러리가 들어 있습니다.",
        "",
        f"■ Pretendard {PRETENDARD_VERSION} — SIL Open Font License 1.1",
        "  화면 글꼴입니다. 고치지 않은 글꼴 파일 셋(Regular·SemiBold·Bold)을 그대로",
        "  포함하며 이 프로그램 안에서만 씁니다(PC에 설치하지 않습니다).",
        "  Copyright (c) 2021, Kil Hyung-jin, with Reserved Font Name Pretendard.",
        f"    소스: {PRETENDARD_URL}",
        "",
        "■ AI 모델 — 실행물에 포함하지 않음",
        "  생성·임베딩 모델은 이 실행물에 들어 있지 않습니다. 사용자가 Ollama로 따로",
        "  받으며, 모델마다 배포처가 정한 이용 조건을 따릅니다. Ollama도 포함하지",
        "  않습니다.",
        "",
        "2. 구성요소 목록",
        "-" * 72,
        "",
    ]
    for role in (RUNTIME, TOOLING):
        group = [item for item in items if item.role == role]
        out.append(f"[{role}] {len(group)}개")
        for item in group:
            out.append(f"  {item.name} {item.version} — {item.license}")
            out.append(f"      {item.url}")
        out.append("")

    missing = missing_texts()
    out += ["3. 라이선스 본문", "-" * 72, ""]
    if missing:
        out += ["아래 라이선스는 구성요소의 배포판에 본문이 들어 있지 않아 이 문서에 싣지",
                "못했습니다. 본문은 다음 주소에 있습니다."]
        out += [f"  {name[:-4]}: {CANONICAL[name]}" for name in missing]
        out.append("")
    for name, text in canonical_texts():
        out += [rule, name[:-4], rule, text, ""]
    text = _python_license()
    if text:
        out += [rule, f"Python {python} — LICENSE.txt", rule, text, ""]
    for item in items:
        for name, text in item.texts:
            out += [rule, f"{item.name} {item.version} — {name}", rule, text, ""]
    return "\n".join(out).rstrip() + "\n"


def write(folder: Path | str = DEFAULT_OUT) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / FILENAME
    path.write_text(render(), encoding="utf-8")
    return path


def load() -> str:
    """화면에 보일 고지문. 실행물에서는 함께 넣어 둔 파일을, 개발 환경에서는
    지금 설치된 것에서 바로 만든 것을 돌려준다."""
    if BUNDLED.is_file():
        return BUNDLED.read_text(encoding="utf-8", errors="replace")
    return render()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(prog="notices", description="오픈소스 고지문을 만든다.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"출력 폴더 (기본: {DEFAULT_OUT})")
    parser.add_argument("--strict", action="store_true",
                        help="본문을 갖추지 못한 라이선스가 있으면 실패로 끝낸다")
    args = parser.parse_args(argv)

    items = components()
    path = write(args.out)
    bundled = sum(item.role == RUNTIME for item in items)
    print(f"{path} — 구성요소 {len(items)}개(실행물 포함 {bundled}개)")
    missing = missing_texts()
    for name in missing:
        print(f"본문 없음: {name[:-4]} — {CANONICAL[name]} 의 내용을 "
              f"{TEXTS_DIR / name} 로 넣어야 한다", file=sys.stderr)
    return 1 if missing and args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
