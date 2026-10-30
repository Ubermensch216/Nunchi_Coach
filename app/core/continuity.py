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

    gaps.extend(_evaluation_gaps(db, items))
    for item in items:
        blocked = closing["id"] if closing and rc.closes_step(item) else None
        if item["kind"] == rc.EVALUATION:
            continue        # 평가 구조는 위에서 한 번에 묻는다
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
    gaps.extend(_official_local_gaps(db, task_id))
    return gaps


def _evaluation_gaps(db, items: list) -> list[dict]:
    """평가 기준이 초안이거나 배점이 맞지 않으면, 확정본을 확인하기 전 자동 채점을 하지 않도록 묻는다."""
    from . import evaluation

    structures = [i for i in items if i["kind"] == rc.EVALUATION and not evaluation.is_check(i)
                  and i["human_state"] not in ws.CONFIRMED_BY]
    checks = [i for i in items if evaluation.is_check(i) and i["human_state"] not in ws.CONFIRMED_BY]
    drafts = [i for i in structures if "초안" in i["content"]]
    if not drafts and not checks:
        return []
    lines = [f"{i['slot'].removeprefix(evaluation.SLOT_PREFIX)}: {i['content']}" for i in drafts]
    lines += [f"{i['slot'].removeprefix(evaluation.CHECK_PREFIX)} — {i['content']}" for i in checks]
    evidence = [(e["doc_id"], e["locator"], e["quote"]) for i in drafts + checks for e in i["evidence"]][:8]
    detail = ("평가 기준이 초안(안)입니다." if drafts else "평가 문서 안에서 배점이 서로 맞지 않습니다.")
    if drafts and checks:
        detail += f" 배점이 맞지 않는 지표도 {len(checks)}개 있습니다."
    return [{
        "gap_key": "evaluation:confirm", "kind": "conflict" if checks else "missing", "field": "평가 확정본",
        "item_id": (checks or drafts)[0]["id"], "step_id": None, "detail": detail,
        "research": " / ".join(lines),
        "next_action": ("확정된 평가계획·지표를 받았는지 시 담당 공문으로 확인하세요. 확정본의 배점과 실적 인정 "
                        "기간을 확인하기 전에는 이 기준으로 자체 점수를 매기지 마세요. 실적 증빙은 지금 모아도 됩니다."),
        "evidence": evidence,
    }]


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


# 엇갈린 값을 누구에게 확인하는가
_WHOM = {rc.DEADLINE: " 문서로 정해지지 않으면 요청 공문을 보낸 부서에 이번 회차 기한을 물으세요.",
         rc.RECIPIENT: " 문서로 정해지지 않으면 요청 공문을 보낸 부서에 제출처를 물으세요.",
         rc.AMOUNT: " 예산·지출 담당 부서의 확정 금액과 견주세요.",
         rc.CONTACT: " 기관 대표 번호나 조직도로 현재 담당을 확인하세요."}


def _conflict(db, item: dict, step_id) -> dict:
    name = rc.KIND_LABEL.get(item["kind"], item["kind"])
    lines = []
    off_map = provenance.official_sources(
        db, [e["doc_id"] for e in item["evidence"] if e.get("doc_id")]
    )
    for evidence in item["evidence"]:
        document = db.document(evidence["doc_id"]) if evidence["doc_id"] else None
        off_info = off_map.get(evidence["doc_id"]) if evidence["doc_id"] else None
        when = (
            (str(off_info["authored_at"])[:10] if off_info and off_info.get("authored_at") else None)
            or ((document["eff_date"] or "시점 미상") if document else "원본 없음")
        )
        official = provenance.describe(off_info)
        source = official or "개인 폴더 자료(PC 파일)"
        lines.append(f"{evidence['quote']} — {evidence['filename']} ({when} · {source})")
    # 무엇이 다른가 · 어디에 영향을 주는가 · 누구에게 확인하는가 · 정해지면 무엇이 바뀌는가(개선계획서 7장)
    detail = f"자료가 서로 다른 값을 적고 있습니다: {item['content']}"
    after = "정해지면 항목을 고쳐 적으면 됩니다."
    if step_id is not None:
        step = db.con.execute("SELECT label FROM work_steps WHERE id = ?", (step_id,)).fetchone()
        if step is not None:
            detail += f" 이 값은 '{step['label']}' 단계를 끝내는 데 필요합니다."
            after = f"정해지면 항목을 고쳐 적으세요 — 그때부터 '{step['label']}' 단계의 완료 기준으로 쓰입니다."
    whom = _WHOM.get(item["kind"], "")
    return {
        "gap_key": f"conflict:{item['kind']}:{item['slot']}", "kind": "conflict",
        "field": f"{name} · {item['slot']}", "item_id": item["id"], "step_id": step_id,
        "detail": detail,
        "research": " / ".join(lines),
        "next_action": ("두 문서의 작성 시점과 성격(등록본·초안·메모)을 견주어 이번 회차에 "
                        f"적용되는 쪽을 확인하세요.{whom} {after} "
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


# ── 공문 ↔ PC 로컬 비교 공백 (P5) ───────────────────────────────────

_REQUEST_TITLE = re.compile(r"요청|제출\s*요령|제출\s*안내|시행\s*계획|추진\s*계획\s*알림|협조\s*요청|의뢰")
_REPLY_TITLE = re.compile(r"회신|제출(?!\s*요청|\s*안내|\s*요령)|결과\s*보고|송부|보고")
_FINAL_LOCAL_MARK = re.compile(r"최종|확정|결재|(?<![A-Za-z])final(?![A-Za-z])", re.IGNORECASE)


def _official_local_gaps(db, task_id: int) -> list[dict]:
    """온나라 공문과 PC 파일을 나란히 놓고 빠진 고리를 찾는다(P5).

      1. unanswered_official: 접수·요청 공문이 있으나 이에 대응하는 회신 공문이나
         PC 제출본이 없는 경우
      2. unregistered_final: PC에 '최종·확정·결재' 파일이 있으나 대응하는 등록 공문이 없는 경우
      3. missing_attachment: 공문에 붙임이 기록되어 있으나 내려받지 못했고 PC 폴더에서도
         대응 파일을 찾지 못한 경우
    """
    from . import docrole, official as official_docs, versions

    rows = db.con.execute(
        "SELECT d.id, d.filename, d.path, d.hash, d.eff_year, d.eff_date, s.kind AS source_kind, "
        "COALESCE(r.role, '') AS role "
        "FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "JOIN sources s ON s.id = d.source_id "
        "LEFT JOIN doc_roles r ON r.doc_id = d.id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL ORDER BY d.id",
        (task_id,),
    ).fetchall()
    if not rows:
        return []

    doc_ids = [row["id"] for row in rows]
    off_sources = provenance.official_sources(db, doc_ids)
    # 이 업무에 공문 기록이 하나도 연결돼 있지 않으면 공문↔로컬 대조 공백을 만들지 않는다.
    records_in_task = db.con.execute(
        f"""
        SELECT DISTINCT r.id, r.document_id, r.title, r.document_number, r.authored_at, r.status
        FROM source_records r
        JOIN source_assets a ON a.record_id = r.id
        WHERE a.doc_id IN ({', '.join('?' * len(doc_ids))})
        ORDER BY COALESCE(r.authored_at, ''), r.id
        """,
        doc_ids,
    ).fetchall()
    if not records_in_task:
        return []

    gaps: list[dict] = []
    local_rows = [row for row in rows if row["source_kind"] != "onnara" and row["id"] not in off_sources]

    # 공문별 본문 문서 ID와 본문 텍스트(인용 문서번호 확인용)
    rec_body_doc: dict[int, int] = {}
    for row in db.con.execute(
        f"SELECT record_id, doc_id FROM source_assets "
        f"WHERE record_id IN ({', '.join('?' * len(records_in_task))}) AND doc_id IS NOT NULL ORDER BY id",
        [r["id"] for r in records_in_task],
    ):
        rec_body_doc.setdefault(row["record_id"], row["doc_id"])

    cited_numbers: set[str] = set()
    for r in records_in_task:
        bdoc = rec_body_doc.get(r["id"])
        if bdoc:
            text = official_docs.body_text(db, [bdoc])
            cited_numbers |= official_docs.numbers_in(text)

    # 1) unanswered_official: 가장 최근 연도의 요청 공문에 회신 공문도, PC 제출본/초안도 없는지 점검
    directives = [
        r for r in records_in_task
        if _REQUEST_TITLE.search(r["title"] or "") and not _REPLY_TITLE.search(r["title"] or "")
    ]
    replies = [r for r in records_in_task if _REPLY_TITLE.search(r["title"] or "")]
    for req in directives:
        req_num = official_docs.normalize_number(req["document_number"] or "")
        req_year = int(req["authored_at"][:4]) if req["authored_at"] and req["authored_at"][:4].isdigit() else None
        has_reply_letter = bool(req_num and req_num in cited_numbers) or any(
            (not req_year or (rep["authored_at"] and rep["authored_at"][:4] == str(req_year)))
            and (not req["authored_at"] or not rep["authored_at"] or rep["authored_at"] >= req["authored_at"])
            for rep in replies
        )
        has_local_submission = any(
            (not req_year or not lrow["eff_year"] or lrow["eff_year"] >= req_year)
            and (
                lrow["role"] in (docrole.SUBMISSION, docrole.PERFORMANCE, docrole.RESULT)
                or _REPLY_TITLE.search(lrow["filename"] or "")
                or _FINAL_LOCAL_MARK.search(lrow["filename"] or "")
                or (req_num and req_num in official_docs.numbers_in(lrow["filename"] or ""))
            )
            for lrow in local_rows
        )
        if not has_reply_letter and not has_local_submission:
            ev_doc = rec_body_doc.get(req["id"])
            num_text = f"({req['document_number']})" if req["document_number"] else ""
            gaps.append({
                "gap_key": f"unanswered_official:{req['document_id']}",
                "kind": "unanswered_official",
                "field": "회신 미확인 공문",
                "item_id": None,
                "step_id": None,
                "detail": (
                    f"요청 공문 ‘{req['title']}’{num_text}은(는) 있으나 이에 대응하는 회신·제출 공문이나 "
                    "PC 제출본을 찾지 못했습니다."
                ),
                "research": f"공문 시행·등록일: {req['authored_at'] or '시점 미상'}",
                "next_action": (
                    "온나라에서 이 공문에 대한 회신·제출 결재 문서가 있는지 확인하거나, "
                    "제출 대상이 아닌 안내 공문이면 ‘확인했습니다’로 닫으세요."
                ),
                "evidence": [(ev_doc, "공문", req["title"])] if ev_doc else [],
            })

    # 2) unregistered_final: PC 폴더에 '최종/확정/결재' 표시가 붙은 파일이 있으나 같은 해 등록 공문이 없는 경우
    vgroups = versions.groups_for(db, doc_ids)
    grouped_with_official = {
        m["doc_id"]
        for g in vgroups
        if any(member["doc_id"] in off_sources for member in g["members"])
        for m in g["members"]
    }
    official_years = {
        int(r["authored_at"][:4])
        for r in records_in_task
        if r["authored_at"] and len(r["authored_at"]) >= 4 and r["authored_at"][:4].isdigit()
    }
    for lrow in local_rows:
        if not _FINAL_LOCAL_MARK.search(lrow["filename"] or ""):
            continue
        if lrow["id"] in grouped_with_official:
            continue
        lyear = lrow["eff_year"]
        # 같은 해에 제출·회신·보고·계획 성격의 등록 공문이 있거나 문서번호가 파일명에 있으면 등록된 것으로 본다.
        nums_in_file = official_docs.numbers_in(lrow["filename"] or "")
        rec_nums = {official_docs.normalize_number(r["document_number"] or "") for r in records_in_task if r["document_number"]}
        if nums_in_file & rec_nums:
            continue
        same_year_registered = any(
            (not lyear or (r["authored_at"] and r["authored_at"][:4] == str(lyear)))
            and not _REQUEST_TITLE.search(r["title"] or "")
            for r in records_in_task
        )
        if same_year_registered and lyear in official_years:
            continue
        gaps.append({
            "gap_key": f"unregistered_final:{lrow['id']}",
            "kind": "unregistered_final",
            "field": "등록 미확인 최종본",
            "item_id": None,
            "step_id": None,
            "detail": (
                f"PC 폴더에 ‘{lrow['filename']}’ 파일(최종·확정·결재 표시)이 있으나, "
                "이에 대응하는 온나라 등록·결재 공문을 찾지 못했습니다."
            ),
            "research": f"파일 시점: {lrow['eff_date'] or lrow['eff_year'] or '시점 미상'} · 개인 폴더 자료",
            "next_action": (
                "실제로 결재·발송까지 마친 문서인지 온나라 문서등록대장에서 확인하세요. "
                "결재 전 초안이면 파일 이름의 최종 표시만 믿지 않도록 유의하세요."
            ),
            "evidence": [(lrow["id"], "파일 이름", lrow["filename"])],
        })

    # 3) missing_attachment: 공문에 붙임이 기록되어 있으나 내려받지 못했고 PC 폴더에서도 찾지 못한 경우
    failed_assets = db.con.execute(
        f"""
        SELECT a.id, a.record_id, a.asset_id, a.filename, a.sha256, a.status, a.error,
               r.title, r.document_number, r.document_id
        FROM source_assets a
        JOIN source_records r ON r.id = a.record_id
        WHERE a.record_id IN ({', '.join('?' * len(records_in_task))})
          AND a.role = 'attachment'
          AND (a.status != 'downloaded' OR a.doc_id IS NULL)
        ORDER BY a.id
        """,
        [r["id"] for r in records_in_task],
    ).fetchall()
    local_hashes = {lrow["hash"] for lrow in local_rows if lrow["hash"]}
    local_names = {(lrow["filename"] or "").casefold() for lrow in local_rows if lrow["filename"]}
    for asset in failed_assets:
        if asset["sha256"] and asset["sha256"] in local_hashes:
            continue
        att_name = (asset["filename"] or "").strip()
        if att_name and att_name.casefold() in local_names:
            continue
        ev_doc = rec_body_doc.get(asset["record_id"])
        label_name = att_name or asset["asset_id"]
        num_text = f"({asset['document_number']})" if asset["document_number"] else ""
        gaps.append({
            "gap_key": f"missing_attachment:{asset['record_id']}:{asset['asset_id']}",
            "kind": "missing_attachment",
            "field": "누락된 공문 붙임",
            "item_id": None,
            "step_id": None,
            "detail": (
                f"공문 ‘{asset['title']}’{num_text}의 붙임 파일 ‘{label_name}’이(가) "
                "수집되지 않았고 PC 자료 폴더에서도 찾지 못했습니다."
            ),
            "research": f"수집 상태: {asset['status'] or '미수집'}" + (f" ({asset['error']})" if asset["error"] else ""),
            "next_action": (
                "온나라에서 해당 공문의 붙임 파일을 다시 내려받거나 전임자 PC의 다른 폴더에 "
                "저장되어 있는지 확인하세요."
            ),
            "evidence": [(ev_doc, "공문 붙임", label_name)] if ev_doc else [],
        })

    return gaps


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

    from . import guidance

    # 안내문 절차에서 우리 단계를 하나라도 가렸으면, 하는 기관이 다르거나 적혀 있지 않은 단계는
    # 우리 행동이 아니다. 그 앞 단계의 결과가 왔는지만 묻는다.
    has_ours = any(step["actor"] == guidance.OURS for step in stated)
    waiting_on = None
    for step in stated:
        if step["run_state"] == ws.DONE or step["blocked"]:
            continue
        if step["actor"] and step["actor"] != guidance.OURS or (has_ours and not step["actor"]):
            waiting_on = waiting_on or step
            continue
        if waiting_on is not None and waiting_on["ordinal"] < step["ordinal"]:
            who = waiting_on["actor"] if waiting_on["actor"] else "다른 기관(안내문에 하는 기관이 적혀 있지 않음)"
            actions.append({"type": "check", "step_id": waiting_on["id"], "gap_id": None,
                            "text": (f"[앞 단계 확인] '{waiting_on['label']}'은(는) {who}이(가) 하는 단계입니다. "
                                     f"이번 회차에 그 결과(받은 공문·자료)가 왔는지 확인하세요.")})
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

    # 새 자료로 일정·낼 곳·하는 법이 바뀌었으면 먼저 알린다(P2).
    from . import changes

    moved = [c for c in changes.latest(db, task_id) if c["kind"] in changes.WATCHED and c["change"] != "added"]
    if moved:
        listing = "; ".join(changes.describe(c) for c in moved[:3])
        more = f" 외 {len(moved) - 3}건" if len(moved) > 3 else ""
        actions.append({"type": "check", "step_id": None, "gap_id": None,
                        "text": (f"[바뀐 자료] 지난 분석 이후 기한·제출처·절차가 바뀌었습니다: {listing}{more}. "
                                 f"새로 들어온 문서를 열어 이번 회차에 맞는 값인지 확인하세요.")})

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
    # 자료가 개정·의결을 말하는 건 — 시행됐는지는 사람이 확인한다.
    enacted = matters.to_confirm(found)
    if enacted:
        from . import matter_trail

        shown = "; ".join(
            f"‘{m['title']}’({matter_trail.describe(next(t for t in m['trail'] if t['signal'] in matter_trail.CANDIDATES))})"
            for m in enacted[:2])
        more = f" 외 {len(enacted) - 2}건" if len(enacted) > 2 else ""
        actions.append({"type": "check", "step_id": None, "gap_id": None, "text": (
            f"[건별 진행 확인] 개정·의결 자료가 있는 건 {len(enacted)}건: {shown}{more}. 개선이 실제로 시행됐는지 "
            f"(공포·시행일, 시스템 반영) 확인해 '건 확인·고치기'에서 '개선 시행 확인'으로 적으세요.")})

    for gap in gaps:
        if gap["next_action"]:
            actions.append({"type": "check", "text": f"[{gap['field']}] {gap['next_action']}",
                            "step_id": gap["step_id"], "gap_id": gap["id"]})
    return actions
