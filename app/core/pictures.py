"""자료 폴더의 그림과 스캔 PDF 가운데 읽을 것을 고른다.

전임자 폴더에는 글자 레이어 없는 PDF(서명 받은 의결서, 결재문서 스캔)와 화면 캡처
(공문 목록, 받은 쪽지)가 있다. 후임자에게 쓸모 있는 것이 여기 들어 있는데 본문을
읽지 못해 빠졌다(실측: 스캔 PDF 290건, 그림 170건).

로컬 모델로 한 쪽 읽는 데 15~40초가 걸린다. 전부 읽을 수는 없으니 고른다.
  · 남의 자료(받은 자료·참고)와 이전 판(old·bak) 폴더의 것은 읽지 않는다.
    타 기관 카드뉴스 수백 건이 여기서 걸러진다.
  · 사진은 읽지 않는다. 이름·폴더에 '사진'이 있거나, 한 폴더에 그림만 여러 장
    모여 있으면 사진·디자인 시안으로 본다.
  · 신분증·통장 사본은 읽지 않는다. 읽으면 주민등록번호와 계좌번호가 검색 색인에
    들어간다.
  · 이름에 '일정·쪽지·주의·메모·캡처·공문'이 있는 그림을 가장 먼저 읽는다 —
    전임자가 일부러 남긴 것이다.
한 번에 정해진 쪽수만 읽고, 남은 것은 다음 분석에서 이어 읽는다.

읽은 글은 원문이 아니라 모델이 읽은 글이다(ingest/vision.py). 항상 '일부만 읽음'으로
남는다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PureWindowsPath

from . import folders
from ..ingest import vision

# 한 번 분석에서 읽을 쪽수. 실측(로컬 gemma4:e2b, CPU)에서 60쪽에 37분이 걸렸다 —
# 다시 분석할 때마다 그만큼 기다리게 할 수는 없어 절반으로 잡는다. 먼저 읽을 것이
# 앞에 오므로 전임자의 캡처는 첫 번에 읽힌다. meta 'vision_pages_per_run'으로 바꾼다.
PAGES_PER_RUN = 30
PAGES_PER_DOCUMENT = 3      # 스캔본은 앞쪽에 요지가 있다
# Windows 글자 인식은 쪽당 1초가 안 걸린다. 사용자가 분량을 정하지 않았으면 넉넉히 읽는다.
PAGES_PER_RUN_FAST = 2000
PAGES_PER_DOCUMENT_FAST = 20
PHOTO_FOLDER_IMAGES = 5     # 한 폴더에 그림이 이만큼 모여 있고 단서가 없으면 사진 묶음이다

_PRIVATE = re.compile(r"신분증|주민등록|통장|여권|면허증|가족관계|인감|등본|초본")
# 카메라·메신저가 붙인 이름(20241126_113857.jpg, Resized_…)도 사진이다.
_PHOTO = re.compile(r"사진|촬영|photo|img[_\-]?\d|dsc[_\-]?\d|kakaotalk|시안|배너|로고"
                    r"|(?<!\d)20\d{6}[_\- ]\d{4,6}|resized_", re.I)
_HINT = re.compile(r"일정|쪽지|주의|메모|캡처|캡쳐|공문|목록|screenshot|화면", re.I)
_ASIDE = {folders.OLD, folders.BACKUP, folders.REFERENCE, folders.RECEIVED}


@dataclass(frozen=True, slots=True)
class Candidate:
    doc_id: int
    path: str
    filename: str
    is_image: bool
    hinted: bool


# 연달아 이만큼 못 읽으면 문서 탓인지 모델 탓인지 점검 그림으로 가린다.
FAILURES_BEFORE_RECHECK = 5


def candidates(db, reader: str | None = None) -> list[Candidate]:
    """읽을 그림·스캔본. 먼저 읽을 것부터.

    '읽어 봤지만 글자가 없었다'고 표시된 문서는 뺀다. reader(vision.reader_mark)를 주면
    그 표시를 **같은 모델·같은 판이 남겼을 때만** 믿는다. 못 읽는 모델이 남긴 표시가
    모델을 바꾼 뒤에도 문서를 막아 두지 않게 하려는 것이다.
    """
    rows = db.con.execute(
        "SELECT d.id, d.path, d.filename, d.ext, d.parse_status, s.path AS root "
        "FROM documents d JOIN sources s ON s.id = d.source_id AND s.kind != 'upload' "
        "WHERE d.missing_since IS NULL AND d.parent_id IS NULL "
        "AND (COALESCE(d.parser, '') != ? OR (? IS NOT NULL AND COALESCE(d.parser_version, '') != ?)) "
        "ORDER BY d.path", (vision.PARSER, reader, reader)).fetchall()
    images_in: Counter = Counter()
    documents_in: Counter = Counter()
    for row in rows:
        folder = str(PureWindowsPath(row["path"]).parent)
        if (row["ext"] or "").lower() in vision.IMAGE_EXTENSIONS:
            images_in[folder] += 1
        elif row["parse_status"] != "skipped":
            documents_in[folder] += 1

    picked: list[Candidate] = []
    for row in rows:
        ext = (row["ext"] or "").lower()
        is_image = ext in vision.IMAGE_EXTENSIONS
        scanned = ext == ".pdf" and row["parse_status"] == "empty"
        if not (scanned or (is_image and row["parse_status"] in ("skipped", "unsupported"))):
            continue
        names = folders.relative_parts(row["path"], row["root"])
        if not _wanted(row["filename"], names, is_image,
                       images_in[str(PureWindowsPath(row["path"]).parent)],
                       documents_in[str(PureWindowsPath(row["path"]).parent)]):
            continue
        hinted = bool(_HINT.search(row["filename"]) or (names and _HINT.search(names[-1])))
        picked.append(Candidate(row["id"], row["path"], row["filename"], is_image, hinted))
    # 전임자가 남긴 캡처 → 스캔한 문서 → 그 밖의 그림
    picked.sort(key=lambda c: (not (c.is_image and c.hinted), c.is_image, c.path))
    return picked


def _wanted(filename: str, names: list[str], is_image: bool, images_here: int,
            documents_here: int) -> bool:
    where = " ".join(names)
    if _PRIVATE.search(filename) or _PRIVATE.search(where):
        return False
    parsed = [folders.parse_part(name) for name in names]
    named = next((i for i, part in enumerate(parsed) if not part.transparent), len(parsed))
    if any(part.modifier in _ASIDE for part in parsed[named + 1:]):
        return False
    if not is_image:
        return True
    hinted = bool(_HINT.search(filename) or (names and _HINT.search(names[-1])))
    if hinted:
        return True
    if _PHOTO.search(filename) or _PHOTO.search(where):
        return False
    # 문서 없이 그림만 여러 장 모인 폴더는 사진이나 디자인 조각이다.
    return not (images_here >= PHOTO_FOLDER_IMAGES and images_here > documents_here)
