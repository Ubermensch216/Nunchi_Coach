"""건의 자취 — 건이 목록 밖의 문서(공문과 그 붙임)에 어떻게 나오는가 (규제개혁 개선계획서 P1-1 남은 일).

건의 진행은 목록(표 계산 문서)만으로는 '목록에 있음 → 취합 목록에 있음 → 회신 칸 있음'까지다.
실제 진행은 공문과 그 붙임에 있다.

  · 부처 검토결과 회신 공문의 붙임: "□ 안 건 명 : 90. (건 제목)" 아래 "□ 검토의견 : □수용 □일부수용
    ■수용곤란" — 칸에 표시한 것이 답이다.
  · 규제개혁위원회 심의결과·의결서: 표의 한 행에 (건 제목) | 개정.
  · 의회 의결 의안 알림: (조례안 이름) | 원안가결.
  · 추진 결과 보고·실적: (건 제목) | 개정.

여기서 하는 일
  · 건 제목(띄어쓰기·기호를 뺀 꼴, 8자 이상)이 나온 문서를 찾는다. 표 계산 문서(목록)는 matters가
    읽으므로 보지 않는다. AI 생성·발간물·압축 묶음 자신(파일 목록)은 근거로 쓰지 않는다.
  · 제목 바로 뒤(다음 행·다음 건 머리 전까지)에서 체크된 검토의견, 의결·심의 결과를 읽는다. 제목과
    떨어진 곳의 말은 다른 건의 것일 수 있으므로 읽지 않는다.
  · 그 문서가 공문이면 그 공문, 아니면 같은 폴더(같은 압축)의 공문을 덮개 공문으로 잇는다 — 시행
    번호와 날짜, 받은 공문인지 보낸 공문인지.
  · 자료가 말하는 데까지만 진행을 올린다: 회신(검토의견)·심의 결과는 진행이고, 의회 의결·결과 보고의
    '개정'은 **시행 근거 후보**다 — 개선이 시행됐는지는 담당자가 확인한다(사람이 정하는 진행).
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PureWindowsPath

from . import authority, matters

ANSWERED, DECIDED, ENACTED, REPORTED, SUBMITTED, MENTIONED = (
    "answered", "decided", "enacted", "reported", "submitted", "mentioned")
SIGNAL_LABEL = {ANSWERED: "회신(검토의견)", DECIDED: "심의·의결 결과", ENACTED: "의회 의결",
                REPORTED: "추진 결과 보고", SUBMITTED: "제출 공문", MENTIONED: "언급"}
# 시행 근거 후보 — 자료가 개정·의결을 말해도 시행은 사람이 확인한다.
CANDIDATES = (ENACTED, REPORTED)

_MIN_KEY = 8
_WINDOW = 8                 # 제목 뒤로 볼 조각 수
_SHORT_CELL = 14            # 결과 칸으로 볼 짧은 조각의 길이
_KEEP_MENTIONS = 30         # 건마다 남길 '언급'의 수
_SPREADSHEETS = (".xlsx", ".xlsm", ".xls")

_LETTER = re.compile(
    r"시행\s+(?P<dept>[가-힣A-Za-z·]{2,20})-(?P<no>\d+)\s*(?:\(\s*(?P<date>\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\.?)\s*\))?"
    r"\s*접수\s*(?:(?P<mine>[가-힣A-Za-z·]{2,20})-(?P<mno>\d+))?")
_SUBJECT = re.compile(r"제\s*목\s+(?P<subject>.{4,90}?)(?:\s+1\.\s|$)")
# 칸에 표시한 것: ■수용곤란, ☑ 수용, ▣일부수용, √ 기시행
_CHECKED = re.compile(r"[■▣☑✔✓√●◉]\s*(?P<word>[가-힣]{2,8})")
_LABELLED = re.compile(r"(?:검토\s*(?:의견|결과)|부처\s*의견|수용\s*여부|조치\s*결과)\s*[:：]?\s*(?P<value>[^□■▣☑\n]{2,30})")
_DECISION = re.compile(r"^(?:원안\s*가결|수정\s*가결|가결|부결|보류|개정|일부\s*개정|폐지|존치|유지|정비|완화|"
                       r"원안\s*의결|수정\s*의결|의결|심의\s*보류)$")
_NEXT_ROW = re.compile(r"^\d{1,3}$|안\s*건\s*명")

_REVIEW_DOC = re.compile(r"검토\s*(?:결과|의견)|부처\s*의견|협의\s*결과|회신")
_COMMITTEE_DOC = re.compile(r"규제개혁위원회|규제\s*심의|심의\s*결과|의결서|서면\s*심의")
_COUNCIL_DOC = re.compile(r"의회|의결\s*의안|조례안|공포")
_RESULT_DOC = re.compile(r"결과\s*보고|추진\s*실적|이행\s*실적|추진\s*결과|실적\s*보고")
_SUBMIT = re.compile(r"제출|건의")


def ready(db) -> bool:
    return db.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'matter_mentions'").fetchone() is not None


def _flat(text: str) -> str:
    return " ".join((text or "").split())


# ── 공문 ────────────────────────────────────────────────────────────

def letter_of(sections) -> dict | None:
    """문서가 공문이면 그 시행 번호·날짜·방향·제목. 아래쪽(마지막) 시행 줄을 쓴다."""
    whole = " ".join(_flat(s["text"]) for s in sections)
    found = None
    for found in _LETTER.finditer(whole):
        pass
    if found is None:
        return None
    if found.group("mine"):
        direction = "internal" if found.group("mine") == found.group("dept") else "received"
    else:
        direction = "outgoing"
    subject = _SUBJECT.search(whole)
    date = re.sub(r"\s+", "", found.group("date") or "").rstrip(".")
    return {"number": f"{found.group('dept')}-{found.group('no')}", "date": date, "direction": direction,
            "subject": _flat(subject.group("subject"))[:80] if subject else ""}


def letter_line(mention: dict) -> str:
    """'받은 공문 지방규제혁신과-1234(2025.7.7.) ‘… 결과 회신’'."""
    if not mention.get("letter"):
        return ""
    word = {"received": "받은 공문", "outgoing": "보낸 공문", "internal": "내부 공문"}.get(mention["direction"], "공문")
    when = f"({mention['letter_date']}.)" if mention.get("letter_date") else ""
    subject = f" ‘{mention['letter_subject']}’" if mention.get("letter_subject") else ""
    return f"{word} {mention['letter']}{when}{subject}"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[가-힣]{2,}", text or "") if w not in ("붙임", "년도", "상반기", "하반기")}


def _cover(document, letters_in_folder: list[dict]) -> dict | None:
    """같은 폴더의 공문 가운데 이 붙임을 보낸 것. 이름이 가장 많이 겹치는 것, 하나뿐이면 그것."""
    if not letters_in_folder:
        return None
    mine = _words(document["filename"])
    scored = sorted(((len(mine & _words(l["subject"] + " " + l["filename"])), l) for l in letters_in_folder),
                    key=lambda pair: -pair[0])
    if scored[0][0] > 0:
        return scored[0][1]
    return letters_in_folder[0] if len(letters_in_folder) == 1 else None


# ── 제목 뒤의 결과 ──────────────────────────────────────────────────

def read_window(window: list[str]) -> tuple[str, str]:
    """제목이 나온 조각과 그 뒤 조각들에서 (결과 낱말, 회신 갈래)를 읽는다. 없으면 ('', '')."""
    for text in window:
        checked = _CHECKED.findall(text)
        if checked and re.search(r"검토|의견|결과|수용", text):
            word = checked[0]
            return word, matters.classify_outcome(word)
        labelled = _LABELLED.search(text)
        if labelled:
            value = labelled.group("value").strip()
            outcome = matters.classify_outcome(value)
            if outcome:
                return value[:20], outcome
    for text in window:
        cell = text.strip()
        if len(cell) <= _SHORT_CELL and _DECISION.match(cell):
            return cell, ""
    return "", ""


def _signal(kind_text: str, word: str, outcome: str, letter: dict | None) -> str:
    if outcome:
        return ANSWERED
    if word and _COUNCIL_DOC.search(kind_text) and "가결" in word:
        return ENACTED
    if word and _COMMITTEE_DOC.search(kind_text):
        return DECIDED
    if word and _RESULT_DOC.search(kind_text):
        return REPORTED
    if letter and letter["direction"] == "outgoing" and _SUBMIT.search(letter["subject"] or ""):
        return SUBMITTED
    return MENTIONED


# ── 잇기 ────────────────────────────────────────────────────────────

def link(db, should_stop=lambda: False) -> dict[str, int]:
    """건 제목이 나온 문서를 찾아 자취를 다시 세운다. 그다음 진행을 다시 매긴다."""
    totals = {"mentions": 0, "answered": 0, "decided": 0, "candidates": 0}
    if not ready(db) or not matters.ready(db):
        return totals
    keys = {}
    for row in db.con.execute("SELECT id, title FROM matters WHERE missing_since IS NULL AND excluded = 0"):
        key = matters.title_key(row["title"])
        if len(key) >= _MIN_KEY:
            keys[row["id"]] = key
    if not keys:
        with db.transaction():
            db.con.execute("DELETE FROM matter_mentions")
            matters.settle(db)
        return totals
    marks = ", ".join("?" * len(_SPREADSHEETS))
    documents = db.con.execute(
        f"SELECT id, path, filename, hash FROM documents WHERE missing_since IS NULL "
        f"AND parse_status IN ('ok', 'partial') AND COALESCE(parser, '') != 'archive' "
        f"AND LOWER(ext) NOT IN ({marks}) ORDER BY id", _SPREADSHEETS).fetchall()
    reference = set(authority.reference_only(db, [d["id"] for d in documents])) if documents else set()
    found: list[dict] = []
    letters: dict[str, list[dict]] = defaultdict(list)
    sections_of: dict[int, list] = {}
    for document in documents:
        if should_stop():
            break
        sections = db.con.execute("SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal",
                                  (document["id"],)).fetchall()
        letter = letter_of(sections)
        folder = str(PureWindowsPath(document["path"]).parent)
        if letter:
            letters[folder].append({**letter, "doc_id": document["id"], "filename": document["filename"]})
        if document["id"] in reference:
            continue
        flat = [matters.title_key(_flat(s["text"])) for s in sections]
        whole = "".join(flat)
        present = {matter_id: key for matter_id, key in keys.items() if key in whole}
        hits = [(matter_id, index) for matter_id, key in present.items()
                for index, text in enumerate(flat) if key in text]
        if not hits:
            continue
        sections_of[document["id"]] = sections
        for matter_id, index in hits:
            found.append({"matter_id": matter_id, "document": document, "index": index, "letter": letter,
                          "folder": folder})
    rows = []
    for hit in found:
        sections = sections_of[hit["document"]["id"]]
        window = [_flat(sections[hit["index"]]["text"])]
        for section in sections[hit["index"] + 1: hit["index"] + 1 + _WINDOW]:
            text = _flat(section["text"])
            if _NEXT_ROW.search(text) or any(k in matters.title_key(text) for k in keys.values()):
                break
            window.append(text)
        word, outcome = read_window(window)
        letter = hit["letter"]
        cover_id = hit["document"]["id"] if letter else None
        if letter is None:
            cover = _cover(hit["document"], [l for l in letters.get(hit["folder"], [])
                                             if l["doc_id"] != hit["document"]["id"]])
            letter, cover_id = (cover, cover["doc_id"]) if cover else (None, None)
        kind_text = " ".join([hit["document"]["filename"], (letter or {}).get("subject", ""),
                              *(_flat(s["text"]) for s in sections[:3])])
        signal = _signal(kind_text, word, outcome, letter)
        rows.append((hit["matter_id"], hit["document"]["id"], sections[hit["index"]]["locator"], signal, word,
                     outcome, cover_id, (letter or {}).get("number", ""), (letter or {}).get("direction", ""),
                     (letter or {}).get("date", ""), (letter or {}).get("subject", ""),
                     " ".join(window)[:240], hit["document"]["hash"]))
    # 같은 건이 한 문서에 여러 번 나오면(표 머리와 본문) 진행을 말하는 것을 앞에 둔다.
    rank = {ANSWERED: 0, DECIDED: 1, ENACTED: 2, REPORTED: 3, SUBMITTED: 4, MENTIONED: 5}
    rows.sort(key=lambda r: (r[0], rank[r[3]], r[1]))
    kept: dict[int, int] = defaultdict(int)
    with db.transaction():
        db.con.execute("DELETE FROM matter_mentions")
        for row in rows:
            if row[3] == MENTIONED:
                if kept[row[0]] >= _KEEP_MENTIONS:
                    continue
                kept[row[0]] += 1
            db.con.execute(
                "INSERT OR IGNORE INTO matter_mentions(matter_id, doc_id, locator, signal, result, outcome, "
                "letter_id, letter, direction, letter_date, letter_subject, quote, hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        matters.settle(db)
    for row in db.con.execute("SELECT signal, COUNT(*) n FROM matter_mentions GROUP BY signal"):
        totals["mentions"] += row["n"]
        if row["signal"] in (ANSWERED, DECIDED):
            totals[row["signal"]] = row["n"]
        if row["signal"] in CANDIDATES:
            totals["candidates"] += row["n"]
    return totals


def for_matter(db, matter_id: int) -> list[dict]:
    """건의 자취 — 진행을 말하는 것부터, 같은 갈래면 공문 날짜 순."""
    if not ready(db):
        return []
    rows = db.con.execute(
        "SELECT m.*, d.filename, d.path, d.hash AS current_hash FROM matter_mentions m "
        "JOIN documents d ON d.id = m.doc_id WHERE m.matter_id = ? AND d.missing_since IS NULL "
        "ORDER BY CASE m.signal WHEN 'answered' THEN 0 WHEN 'decided' THEN 1 WHEN 'enacted' THEN 2 "
        "WHEN 'reported' THEN 3 WHEN 'submitted' THEN 4 ELSE 5 END, m.letter_date DESC, m.doc_id",
        (matter_id,)).fetchall()
    return [{**dict(r), "current": r["hash"] == r["current_hash"]} for r in rows]


def describe(mention: dict) -> str:
    """'회신(검토의견) 수용곤란 — 받은 공문 지방규제혁신과-1234(2025.7.7.) · (붙임3) 검토결과.hwp'."""
    head = SIGNAL_LABEL[mention["signal"]] + (f" {mention['result']}" if mention["result"] else "")
    tail = " · ".join(p for p in (letter_line(mention), mention["filename"]) if p)
    return f"{head} — {tail}"


def evidence(mention: dict) -> dict:
    return {"doc_id": mention["doc_id"], "locator": mention["locator"], "quote": mention["quote"],
            "filename": mention["filename"], "path": mention["path"], "current": mention["current"]}
