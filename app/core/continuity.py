"""정보 공백 처리 — 모르는 것이 있어도 일이 멈추지 않게 한다 (구축계획서 C04, §4.3).

'미확인' 표시만 늘어놓으면 분석을 후임자에게 되돌려 주는 것이다. 공백마다
세 가지를 붙인다.

    무엇이 비었나      어느 항목이, 왜 정할 수 없는가
    다시 찾아본 결과   이 업무 밖의 관련 문서를 한 번 더 뒤진 결과
    다음 행동          어떤 문서의 어디를 보면 되는가 — 구체적으로

그리고 공백은 **자기가 걸린 단계만** 막는다. 제출 기한을 모른다고 자료 취합을
못 하는 것은 아니다.

지키는 것
  · 다시 찾는 것은 공백마다 한 번, 정해진 문서 수 안에서다. 못 찾으면 못
    찾았다고 적고 끝낸다. 질의를 바꿔 가며 계속 뒤지지 않는다.
  · 다시 찾아서 나온 값을 항목으로 올리지 않는다. 그 문서는 이 업무에 묶여
    있지 않다 — 이 업무의 자료인지는 사람이 정한다. 후보로만 보여 준다.
  · 자료가 엇갈리면 양쪽의 문서·시점·출처를 나란히 놓는다. 어느 쪽이 맞는지
    고르지 않는다.
  · '전임자에게 문의'로 끝내지 않는다. 전임자가 없다는 것이 이 제품의 전제다.
"""

from __future__ import annotations

import re
from datetime import date

from . import korean, provenance, reconstruction as rc, workstate as ws

# 이것이 없으면 일을 끝낼 수 없는 항목들. 나머지 종류는 없어도 공백으로 치지 않는다.
ESSENTIAL = (rc.DEADLINE, rc.RECIPIENT, rc.PROCEDURE)
# 제출 단계에 걸리는 항목. 이 항목의 공백만 그 단계를 막는다.
_CLOSING_KINDS = (rc.DEADLINE, rc.RECIPIENT)
RESEARCH_DOC_LIMIT = 20
_MAX_FOUND = 3


# ── 한 번의 재검색 ──────────────────────────────────────────────────

def research(db, task_id: int, kind: str) -> tuple[list[rc.Found], int]:
    """이 업무에 묶이지 않은 관련 문서에서 그 종류의 값을 한 번 찾아본다.

    (찾은 값, 살펴본 문서 수)를 돌려준다. 관련 문서란 그 이름표가 본문에
    있으면서, 어느 업무에도 묶이지 않았거나 업무 이름의 낱말이 들어 있는 문서다.
    """
    task = db.task(task_id)
    words = [korean.stem(word) for word in korean.tokens(task["name"] if task else "")]
    labels = rc._LABELS[kind]
    label_filter = " OR ".join("i.body LIKE ?" for _ in labels)
    name_filter = " OR ".join("(i.body LIKE ? OR i.filename LIKE ?)" for _ in words)
    relation = "NOT EXISTS (SELECT 1 FROM task_docs x WHERE x.doc_id = d.id)"
    if words:
        relation = f"({relation} OR {name_filter})"
    params: list = [task_id, *(f"%{label}%" for label in labels)]
    for word in words:
        params += [f"%{word}%", f"%{word}%"]

    documents = db.con.execute(
        f"SELECT d.id, d.eff_year FROM documents d JOIN document_index i ON i.doc_id = d.id "
        f"WHERE d.missing_since IS NULL AND d.parse_status IN ('ok', 'partial') "
        f"  AND NOT EXISTS (SELECT 1 FROM task_docs td WHERE td.task_id = ? AND td.doc_id = d.id) "
        f"  AND ({label_filter}) AND {relation} "
        f"ORDER BY d.eff_date DESC NULLS LAST, d.id LIMIT {RESEARCH_DOC_LIMIT}",
        params).fetchall()
    official = provenance.official_sources(db, [row["id"] for row in documents])

    found: list[rc.Found] = []
    for document in documents:
        for section in db.con.execute(
            "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal",
            (document["id"],),
        ):
            found.extend(item for item in rc.read_section(
                section["text"], document["id"], section["locator"], document["eff_year"],
                official=document["id"] in official) if item.kind == kind)
    return found[:_MAX_FOUND], len(documents)


# ── 공백 찾기 ───────────────────────────────────────────────────────

def find_gaps(db, task_id: int, basis: date | None = None,
              candidates: list[dict] | None = None) -> list[dict]:
    """이 업무의 공백을 다음 행동과 함께 만든다. 저장하지 않는다.

    `candidates`는 방금 자료에서 읽은 항목 후보다(없으면 다시 읽는다). 이전 시기의
    다른 값은 저장된 항목에 없고 후보에만 실려 있다.
    """
    basis = basis or date.today()
    from . import bundles

    if bundles.kind(db, task_id):
        return []       # 일이 아닌 묶음에 기한·제출처·절차를 묻지 않는다
    if candidates is None:
        candidates = rc.extract_task(db, task_id, basis)
    earlier = {(c["kind"], c["slot"]): c.get("earlier") or [] for c in candidates}
    items = db.task_items(task_id)
    steps = db.work_steps(task_id)
    by_kind: dict[str, list[dict]] = {}
    for item in items:
        by_kind.setdefault(item["kind"], []).append(item)
    # 문서에 적힌 단계와 사람이 알려 준 단계. 과거 사례는 순서로 세지 않는다.
    stated = [s for s in steps if s["procedure_kind"] != ws.PROCEDURE_PRECEDENT]
    cases = [s for s in steps if s["procedure_kind"] == ws.PROCEDURE_PRECEDENT]
    closing = next((s for s in reversed(stated) if rc._SUBMITS.search(s["label"])), None)
    docs_in_task = db.con.execute(
        "SELECT COUNT(*) FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL", (task_id,)).fetchone()[0]
    context = _Context(db, task_id, by_kind, cases, docs_in_task)

    shape = _shape(db, task_id)
    gaps: list[dict] = []
    for kind in ESSENTIAL:
        if kind == rc.PROCEDURE and (stated or (shape["classified"] and len(cases) >= 2)):
            # 지난 회차에 어떤 순서로 했는지가 둘 이상 남아 있으면 따라 할 순서가 있다.
            continue
        if kind in _CLOSING_KINDS and shape["classified"] and not shape["requested"]:
            # 어디서 요청받아 내는 일이 아니다. 요청·시행 공문이 없는 업무에 '제출처를
            # 모른다'고 적으면 업무마다 같은 공백이 붙어 정말 확인할 것이 묻힌다
            # (실측: 공백 750건 중 732건이 업무마다 똑같은 네 가지였다).
            continue
        if not by_kind.get(kind):
            gaps.append(_missing(context, kind, closing))

    for item in items:
        blocked = closing["id"] if closing and rc.closes_step(item) else None
        if item["human_state"] in ws.CONFIRMED_BY:
            # 사람이 값을 정했다. 자료가 여전히 엇갈려도 더 묻지 않는다 —
            # 그 뒤에 자료가 또 달라지면 '재확인 필요'가 되어 다시 걸린다.
            continue
        if earlier.get((item["kind"], item["slot"])) and item["basis_state"] == ws.STATED:
            gaps.append(_changed(db, item, earlier[(item["kind"], item["slot"])]))
        if item["basis_state"] == ws.CONFLICT:
            gaps.append(_conflict(db, item, blocked))
        elif item["kind"] == rc.DEADLINE and item["applicability"] == ws.PAST:
            if rc.is_schedule(item["slot"]):
                # 지난 회차 추진일정의 한 줄이다. 지난 것이 당연하고, 올해 일정의
                # 선례로 보이면 된다. 줄마다 '기한이 지났다'고 묻지 않는다.
                continue
            gaps.append(_past_deadline(context, item, blocked, basis))
        elif item["kind"] == rc.RECORD and item["slot"] == rc.SLOT_AFTERWARDS:
            if shape["classified"] and shape["one_off"]:
                continue    # 한 번 있었던 일에 '이번 회차를 시작했는지'를 묻지 않는다
            gaps.append(_afterwards(context, item))
    return gaps


def _shape(db, task_id: int) -> dict:
    """이 업무가 어떤 꼴의 일인지 — 공백을 물을지 말지 정하는 데만 쓴다.

    문서 갈래가 아직 정해지지 않았으면(classified=False) 아무것도 걸러 내지 않는다.
    """
    roles = [row["role"] for row in db.con.execute(
        "SELECT r.role FROM task_docs td JOIN doc_roles r ON r.doc_id = td.doc_id "
        "WHERE td.task_id = ?", (task_id,))]
    years = db.con.execute(
        "SELECT COUNT(DISTINCT d.eff_year) FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL AND d.eff_year IS NOT NULL "
        "AND d.eff_date_kind != 'fs'", (task_id,)).fetchone()[0]
    recurring = db.con.execute(
        "SELECT 1 FROM task_cycles WHERE task_id = ? AND kind != 'none'", (task_id,)).fetchone()
    return {"classified": bool(roles), "requested": "directive" in roles,
            "one_off": years <= 1 and not recurring}


# 공백마다 붙는 안내. 화면은 이 문장을 줄마다 되풀이하지 않고 한 번만 적기
# 위해 이 이름으로 찾아낸다(ui/views/tasks.py) — 보고서·초안에는 그대로 나간다.
ONNARA_HINT = "온나라에서 전임자 작성 문서를 수집하면 등록된 공문이 근거로 더해집니다."


class _Context:
    """공백 문구를 만들 때 함께 보는 이 업무의 사정."""

    def __init__(self, db, task_id, by_kind, cases, docs_in_task):
        self.db = db
        self.task_id = task_id
        self.by_kind = by_kind
        self.cases = cases
        self.docs_in_task = docs_in_task

    def recipient(self) -> str:
        item = next(iter(self.by_kind.get(rc.RECIPIENT, [])), None)
        return item["content"] if item and item["basis_state"] == ws.STATED else ""

    def onnara_hint(self) -> str:
        """온나라 수집을 한 번도 안 했을 때만 권한다. 이미 했으면 같은 말을 반복하지 않는다."""
        collected = self.db.con.execute("SELECT 1 FROM collection_runs LIMIT 1").fetchone()
        return "" if collected else f" {ONNARA_HINT}"

    def case_hint(self) -> str:
        if not self.cases:
            return ""
        first = self.cases[0]
        return (f" 문서에 적힌 절차가 없어도 지난 회차의 수행 사례 {len(self.cases)}단계"
                f"(첫 단계: {first['label']})를 참고해 준비를 시작할 수 있습니다.")


def _missing(context: _Context, kind: str, closing) -> dict:
    name = rc.KIND_LABEL[kind]
    found, looked = research(context.db, context.task_id, kind)
    evidence = [(item.doc_id, item.locator, item.quote) for item in found]
    if found:
        names = {row["id"]: row["filename"] for row in context.db.con.execute(
            f"SELECT id, filename FROM documents WHERE id IN "
            f"({', '.join('?' * len(found))})", [item.doc_id for item in found])}
        listing = "; ".join(f"{names.get(item.doc_id, '')} — {item.slot}: {item.value}"
                            for item in found)
        researched = (f"이 업무에 묶이지 않은 문서에서 {name} 표기를 찾았습니다: {listing}. "
                      f"이 업무의 자료인지는 확인되지 않았습니다.")
        action = ("위 문서를 열어 이 업무의 것인지 확인하세요. 맞으면 문서를 이 업무에 "
                  "배정하면 다음 분석에서 항목으로 올라옵니다.")
    else:
        researched = (f"이 업무의 문서 {context.docs_in_task}건과 관련 문서 {looked}건에서 "
                      f"{name} 표기({', '.join(rc._LABELS[kind][:3])} 등)를 찾지 못했습니다.")
        action = _where_to_look(context, kind)
    return {
        "gap_key": f"missing:{kind}", "kind": "missing", "field": name,
        "detail": f"{name}이(가) 이 업무의 자료에 적혀 있지 않습니다.",
        "research": researched, "next_action": action, "evidence": evidence,
        # 기한·제출처를 몰라도 그 앞 단계는 할 수 있다. 내는 단계만 걸린다.
        "step_id": closing["id"] if closing and kind in _CLOSING_KINDS else None,
    }


def _where_to_look(context: _Context, kind: str) -> str:
    if kind == rc.DEADLINE:
        text = "이번 회차의 요청·시행 공문 본문과 붙임에서 기한을 확인하세요."
        if context.recipient():
            text += f" 공문이 없으면 제출처({context.recipient()})에 이번 회차 일정을 확인하세요."
        return text + context.onnara_hint()
    if kind == rc.RECIPIENT:
        text = "요청 공문의 발신 부서와, 지난 회차 제출본의 수신 표기를 확인하세요."
        submitted = next((case for case in reversed(context.cases)
                          if rc._SUBMITS.search(case["label"]) and case["output"]), None)
        if submitted:
            text += f" 지난 회차 제출 기록: {submitted['output']}."
        return text + context.onnara_hint()
    guides = manuals(context.db, context.task_id)
    if guides:
        return (f"이 업무에 묶인 안내 자료 {guide_names(guides)}에 처리 절차가 있는지 먼저 확인하세요."
                + context.case_hint() + context.onnara_hint())
    return ("지침·계획·매뉴얼 문서에 처리 절차가 적혀 있는지 확인하세요."
            + context.case_hint() + context.onnara_hint())


def _conflict(db, item: dict, step_id) -> dict:
    name = rc.KIND_LABEL.get(item["kind"], item["kind"])
    lines = []
    for evidence in item["evidence"]:
        document = db.document(evidence["doc_id"]) if evidence["doc_id"] else None
        when = (document["eff_date"] or "시점 미상") if document else "원본 없음"
        official = provenance.describe(
            provenance.official_sources(db, [evidence["doc_id"]]).get(evidence["doc_id"]))
        source = official or "개인 폴더 자료"
        lines.append(f"{evidence['quote']} — {evidence['filename']} ({when} · {source})")
    return {
        "gap_key": f"conflict:{item['kind']}:{item['slot']}", "kind": "conflict",
        "field": f"{name} · {item['slot']}", "item_id": item["id"], "step_id": step_id,
        "detail": f"자료가 서로 다른 값을 적고 있습니다: {item['content']}",
        "research": " / ".join(lines),
        "next_action": ("두 문서의 작성 시점과 성격(등록본·초안·메모)을 견주어 이번 회차에 "
                        "적용되는 쪽을 확인하세요. 정해지면 항목을 고쳐 적으면 됩니다. "
                        "확정 전에도 이 값이 필요 없는 단계는 진행할 수 있습니다."),
        "evidence": [(e["doc_id"], e["locator"], e["quote"]) for e in item["evidence"]],
    }


def _changed(db, item: dict, earlier: list[dict]) -> dict:
    """최근 자료의 값을 올렸지만, 그 이전 자료는 다르게 적고 있다.

    개정된 지침이 대표적이다. 최근 문서가 대개 맞지만 '대개'다 — 경과 조치나
    적용 시점에 따라 이전 기준이 적용되는 건이 있다. 그래서 값을 숨기지도,
    최근 것이라고 확정하지도 않고 둘을 나란히 보인다. 어떤 단계도 막지 않는다.
    """
    name = rc.KIND_LABEL.get(item["kind"], item["kind"])
    names = {row["id"]: row["filename"] for row in db.con.execute(
        f"SELECT id, filename FROM documents WHERE id IN ({', '.join('?' * len(earlier))})",
        [old["doc_id"] for old in earlier])}
    before = "; ".join(f"{old['year']}년 ‘{old['value']}’ — {names.get(old['doc_id'], '')}"
                       for old in earlier)
    return {
        "gap_key": f"changed:{item['kind']}:{item['slot']}", "kind": "changed",
        "field": f"{name} · 이전과 다름", "item_id": item["id"], "step_id": None,
        "detail": (f"가장 최근 자료의 값 ‘{item['content']}’을(를) 올렸습니다. 그 이전 자료는 "
                   f"다르게 적고 있습니다. 최근 문서라는 이유만으로 이번 건에 적용된다고 "
                   f"확정하지 않았습니다."),
        "research": f"이전 자료: {before}",
        "next_action": ("최근 문서에 시행일이나 적용 대상이 적혀 있는지 확인하세요. 이번 건이 "
                        "그 이전에 시작됐거나 경과 조치 대상이면 이전 기준이 적용될 수 "
                        "있습니다. 확인되면 항목에 ‘맞습니다’를 누르면 됩니다."),
        "evidence": ([(e["doc_id"], e["locator"], e["quote"]) for e in item["evidence"]]
                     + [(old["doc_id"], old["locator"], old["quote"]) for old in earlier]),
    }


def _past_deadline(context: _Context, item: dict, step_id, basis: date) -> dict:
    if item["slot"].startswith(rc.EXPIRY_PREFIX):
        # 임기·유효기간이 끝났다. 지난 회차의 기한이 아니라 지금 확인할 일이다.
        what = item["slot"].removeprefix(rc.EXPIRY_PREFIX)
        return {
            "gap_key": f"expired:{item['slot']}", "kind": "stale",
            "field": f"만료 · {what}", "item_id": item["id"], "step_id": None,
            "detail": (f"자료에 적힌 {what}이(가) {item['content']}에 끝났습니다(기준일 {basis.isoformat()}). "
                       f"그 뒤에 다시 정했는지 자료로 알 수 없습니다."),
            "research": None,
            "next_action": f"{what}을(를) 다시 정했는지(재위촉·연장·갱신) 확인하세요." + context.onnara_hint(),
            "evidence": [(e["doc_id"], e["locator"], e["quote"]) for e in item["evidence"]],
        }
    action = "이번 회차의 요청 공문이 새로 왔는지 확인하세요."
    if context.recipient():
        action += f" 없으면 제출처({context.recipient()})에 이번 회차 일정을 확인하세요."
    return {
        "gap_key": f"stale:{rc.DEADLINE}:{item['slot']}", "kind": "stale",
        "field": f"기한 · {item['slot']}", "item_id": item["id"], "step_id": step_id,
        "detail": (f"기록상 기한 {item['content']}은(는) 기준일({basis.isoformat()}) 이전입니다. "
                   f"이미 지난 회차의 기한인지, 이번 회차가 늦어진 것인지 자료로 알 수 없습니다."),
        "research": None,
        "next_action": action + context.onnara_hint(),
        "evidence": [(e["doc_id"], e["locator"], e["quote"]) for e in item["evidence"]],
    }


def _afterwards(context: _Context, item: dict) -> dict:
    """마지막 기록 뒤가 비어 있다. 지난 회차에서 그다음에 무엇을 했는지로 실마리를 준다."""
    last_doc = item["evidence"][0]["doc_id"] if item["evidence"] else None
    action = "마지막 기록 이후에 만든 파일이 폴더에 있는지, 결재·발송 문서가 있는지 확인하세요."
    position = next((index for index, case in enumerate(context.cases)
                     if any(e["doc_id"] == last_doc for e in case["evidence"])), None)
    if position is None:
        # 지난 회차의 사례라면 문서는 다르다. 마지막 기록과 같은 종류의 단계를 찾는다.
        record = next((r for r in context.by_kind.get(rc.RECORD, [])
                       if r["slot"] == rc.SLOT_LAST_RECORD), None)
        stage = record["content"].split(" · ", 1)[-1].split(" — ")[0] if record else ""
        position = next((index for index, case in enumerate(context.cases)
                         if stage and case["label"].startswith(stage)), None)
    if position is not None and position + 1 < len(context.cases):
        following = context.cases[position + 1]
        action = (f"지난 기록에서는 그다음에 '{following['label']}'을(를) 했습니다. 이에 "
                  f"해당하는 이번 회차 파일({following['output'] or '산출물'}과 같은 종류)이 "
                  f"있는지 확인하세요.")
    return {
        "gap_key": "afterwards", "kind": "missing", "field": "이후 상태",
        "item_id": item["id"], "step_id": None,
        "detail": item["content"], "research": None,
        "next_action": action + context.onnara_hint(),
        "evidence": [(e["doc_id"], e["locator"], e["quote"]) for e in item["evidence"]],
    }


def review_task(db, task_id: int, basis: date | None = None,
                candidates: list[dict] | None = None) -> dict[str, int]:
    """공백을 다시 찾아 저장된 자동 공백과 맞춘다."""
    return db.sync_info_gaps(task_id, find_gaps(db, task_id, basis, candidates))


# ── 다음 행동 ───────────────────────────────────────────────────────

# 운영 매뉴얼·지침의 이름. 처리 절차가 화면·장 단위로 적혀 있어 '절차:' 이름표로는 읽히지
# 않지만, 절차를 찾을 첫 자료다.
_GUIDE = re.compile(r"매뉴얼|편람|길라잡이|핸드북|가이드|안내서|작성\s*요령|처리\s*요령|사용\s*설명|업무\s*지침")
_SCORING = re.compile(r"지표|평가|배점|실적\s*산출")
_MAX_GUIDES = 2


def manuals(db, task_id: int) -> list:
    """이 업무에 묶인 운영 매뉴얼·지침 (최근 것부터). AI 생성 참고자료·발간물은 뺀다."""
    from . import authority

    rows = db.con.execute(
        "SELECT d.id, d.filename, d.path, d.eff_year FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "LEFT JOIN doc_roles r ON r.doc_id = d.id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL AND d.parse_status != 'skipped' "
        "  AND COALESCE(r.role, '') IN ('criteria', 'legal', 'directive', 'other', '') "
        "ORDER BY COALESCE(d.eff_year, 0) DESC, d.id", (task_id,)).fetchall()
    # 평가 지표 매뉴얼은 실적을 세는 법이지 일을 처리하는 법이 아니다. 운영 매뉴얼을 앞에 둔다.
    rows = sorted(rows, key=lambda row: bool(_SCORING.search(row["filename"] or "")))
    picked, seen = [], set()
    for row in rows:
        stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", row["filename"] or "")
        if not _GUIDE.search(stem) or authority.marker(row["filename"] or "", row["path"] or ""):
            continue
        if stem in seen:
            continue        # 같은 매뉴얼의 PDF·한글 판
        seen.add(stem)
        picked.append(row)
    return picked[:_MAX_GUIDES]


def guide_names(guides) -> str:
    """'‘신문고 매뉴얼.pdf’(2025년 자료)' — 몇 년 자료인지 함께 밝힌다. 지난 판일 수 있다."""
    return ", ".join(f"‘{row['filename']}’" + (f"({row['eff_year']}년 자료)" if row["eff_year"] else "")
                     for row in guides)


def _runnable(step) -> bool:
    """이 단계를 지금 '진행할 수 있다'고 안내해도 되는가.

    사람이 확인했거나 알려 준 단계는 된다. 문서에 적힌 단계는 그 문서가 지난 회차의 것이
    아닐 때만 된다 — 확인되지 않은 과거 절차를 현재의 실행 안내로 바꾸지 않는다.
    """
    if step["human_state"] in ws.CONFIRMED_BY or step["procedure_kind"] == ws.PROCEDURE_TOLD:
        return True
    return step["applicability"] != ws.PAST


def next_actions(db, task_id: int) -> list[dict]:
    """지금 할 수 있는 일과 확인할 일. 화면의 '다음 행동'에 그대로 쓴다.

    `do`가 먼저 온다 — 확인할 것이 남아 있어도 진행할 수 있는 일이 있으면
    그것부터 보여 준다.
    """
    steps = db.work_steps(task_id)
    gaps = db.info_gaps(task_id)
    stated = [s for s in steps if s["procedure_kind"] != ws.PROCEDURE_PRECEDENT]
    actions: list[dict] = []

    for step in stated:
        if step["run_state"] == ws.DONE or step["blocked"]:
            continue
        if not _runnable(step):
            # 지난 회차 문서에 적힌 절차다. 그때의 절차가 이번에도 맞는지 모르는 채로
            # '진행할 수 있습니다'라고 하지 않는다(R01). 무엇을 확인할지를 말한다.
            text = (f"문서에 적힌 절차는 지난 회차 자료에서 읽은 것입니다. 이번 회차의 요청 공문이나 "
                    f"현행 지침으로 같은 절차인지 확인한 뒤 '{step['label']}'부터 진행하세요.")
            actions.append({"type": "check", "text": f"[현 회차 적용 확인] {text}",
                            "step_id": step["id"], "gap_id": None})
            break
        text = f"'{step['label']}' 단계를 진행할 수 있습니다."
        if step["inputs"]:
            text += f" 준비 자료: {step['inputs']}."
        if step["forms"]:
            text += f" 서식: {step['forms']}."
        actions.append({"type": "do", "text": text, "step_id": step["id"], "gap_id": None})
        break
    if not stated:
        # 적힌 절차가 없어도 이 업무에 묶인 공식 운영 매뉴얼이 있으면 그것이 첫 자료다.
        # 지난 회차에 다룬 건(사례)의 참고자료보다 앞선다(R04: 신문고 매뉴얼을 두고
        # 근로시간 정책 보고서를 먼저 읽으라고 안내했다).
        guides = manuals(db, task_id)
        if guides:
            actions.append({"type": "do", "step_id": None, "gap_id": None,
                            "text": (f"이 업무 자료에서 처리 절차를 이름표로 읽지 못했습니다. 먼저 업무 "
                                     f"안내 자료 {guide_names(guides)}에서 처리 절차와 작성 방법을 확인하세요.")})
        cases = [s for s in steps if s["procedure_kind"] == ws.PROCEDURE_PRECEDENT]
        # 이미 했다고 기록한 사례는 다시 권하지 않는다.
        todo = next((case for case in cases if case["run_state"] != ws.DONE), None)
        if todo is not None:
            where = "부터 했습니다" if todo is cases[0] else "을(를) 그다음에 했습니다"
            text = (f"문서에 적힌 절차는 없습니다. 지난 회차에는 '{todo['label']}'{where} — "
                    f"그때의 자료({todo['output'] or '근거 문서'})를 열어 "
                    f"준비할 수 있습니다.")
            actions.append({"type": "do", "text": text, "step_id": todo["id"], "gap_id": None})

    # 이번 회차 건 가운데 회신이 이어지지 않은 것. 건마다 소관부서·접수·회신을 따로 챙긴다(W02).
    from . import matters

    found = matters.for_task(db, task_id)
    waiting = matters.unanswered(found)
    if waiting:
        names = ", ".join(f"‘{m['title']}’" for m in waiting[:3])
        more = f" 외 {len(waiting) - 3}건" if len(waiting) > 3 else ""
        when = waiting[0]["round"] or f"{waiting[0]['year']}년"
        awaited = " 또는 ".join(dict.fromkeys(matters.AWAITED.get(m["kind"], "결과") for m in waiting))
        text = (f"[건별 진행 확인] {when} 우리 기관 건 {len(waiting)}건은 다음 결과가 자료에 이어지지 "
                f"않았습니다(기다리는 결과: {awaited}): {names}{more}. 제출·접수 문서와 결과 문서를 찾아 건마다 진행을 확인하세요. "
                f"목록에 있다는 것만으로 제출·수용을 확정하지 않습니다.")
        twins = [(m, matters.lookalike(m, found)) for m in waiting]
        twins = [(m, twin) for m, twin in twins if twin]
        if twins:
            text += " 제목이 바뀌어 따로 잡혔을 수 있는 건: " + "; ".join(
                f"‘{m['title']}’ ↔ ‘{twin['title']}’({matters.progress_line(twin)})" for m, twin in twins[:2]) + "."
        actions.append({"type": "check", "step_id": None, "gap_id": None, "text": text})

    for gap in gaps:
        if gap["next_action"]:
            actions.append({"type": "check", "text": f"[{gap['field']}] {gap['next_action']}",
                            "step_id": gap["step_id"], "gap_id": gap["id"]})
    return actions
