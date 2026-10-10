"""A3·A4 — 연도별 비교와 세 가지 결과물 (계획서 2단계).

만드는 것
  연도별 비교 보고서   해별로 같은 구조에 담아 칸끼리 견준 것. 모든 결론에 근거 번호
  관련 문서 근거목록   쓴 문서·후보·의심·읽지 못한 문서와 그 사유
  올해 확인 필요사항   올해 기준이 확인됐는가, 비교에서 확정하지 못한 것, 지난 지적사항

본문은 규칙이 쓴다. 생성 모델을 부르지 않으므로 같은 자료에서는 같은 글이 나온다.

지키는 것
  · 대상 연도(올해) 칸은 올해 공식 문서가 있을 때만 값이 채워진다. 없으면 '미확인'이다.
  · 과거 해의 기한·배점은 '과거 값'으로 표시한다. 올해 값처럼 읽히는 표현을 쓰지 않는다.
  · 비교하지 못한 영역은 '견주지 않았습니다'라고 적는다.
"""

from __future__ import annotations

from ...core import docrole, docyears, yearcompare as yc
from .. import policy
from ..registry import REGISTRY, Tool
from . import common

REPORT_TITLE, EVIDENCE_TITLE, QUESTIONS_TITLE = "연도별 비교 보고서", "관련 문서 근거 목록", "올해 확인 필요사항"
QUOTE_CHARS = 110
PAST = "과거 값"


class Notes:
    """근거 번호. 같은 대목은 같은 번호를 쓴다."""

    def __init__(self, db) -> None:
        self.db, self._order, self._numbers = db, [], {}

    def mark(self, refs) -> str:
        marks = []
        for ref in refs:
            key = (ref.doc_id, ref.locator, ref.quote)
            if key not in self._numbers:
                self._order.append(ref)
                self._numbers[key] = len(self._order)
            marks.append(f"[{self._numbers[key]}]")
        return "".join(dict.fromkeys(marks))

    def lines(self) -> list[str]:
        out = []
        for number, ref in enumerate(self._order, 1):
            quote = ref.quote if len(ref.quote) <= QUOTE_CHARS else ref.quote[:QUOTE_CHARS] + "…"
            out.append(f"{number}. {common.ref_text(self.db, ref)}" + (f" — “{quote}”" if quote else ""))
        return out

    @property
    def refs(self):
        return list(self._order)


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out


def _target_lines(target: common.Target) -> list[str]:
    year = target.year
    if target.official:
        lines = [f"- {year}년 평가 기준: **확인됨** — {target.how}"]
    else:
        lines = [f"- {year}년 평가 기준: **미확인** — {target.how}.",
                 f"  아래의 과거 해 내용은 {year}년 기준이 아니라 **참고**입니다."]
    if target.due:
        label = "공식 기한" if target.due_source == "official" else "담당자가 정한 기한"
        lines.append(f"- {year}년 제출기한: {target.due.isoformat()} ({label})")
    else:
        lines.append(f"- {year}년 제출기한: **확인 필요**" + (f" — {target.due_note}" if target.due_note else ""))
    return lines


def render_report(db, name: str, cmp: yc.YearComparison, target: common.Target, today) -> tuple[str, list]:
    notes = Notes(db)
    mode = "공식 기준 반영" if target.official else "초안 · 사전조사용 (올해 공식 기준 미확인)"
    out = [f"# {name} — 연도별 비교 보고서", "",
           f"만든 날 {today.isoformat()} · 대상 연도 {target.year}년 · 상태 **{mode}**", "",
           "> 업무에 묶인 문서에서 읽은 내용을 해별로 같은 구조에 담아 칸끼리 견준 것입니다. "
           f"과거 해의 기준을 {target.year}년 기준으로 단정하지 않습니다.", "",
           f"## 0. 올해({target.year}년) 기준", *_target_lines(target), ""]

    if not cmp.years:
        out += ["## 1. 견준 해", "견줄 수 있는 해가 없습니다. 연도가 적힌 시행문·지표·서식·실적 문서가 필요합니다.", ""]
    else:
        out += ["## 1. 견준 해", ", ".join(f"{y}년" for y in cmp.years)
                + (f" · {target.year}년은 올해 공식 문서가 없어 견주지 않았습니다" if not target.official else ""), ""]
    if not cmp.pairs and len(cmp.years) == 1:
        out += [f"견줄 두 해가 없습니다. {cmp.years[0]}년 한 해의 자료만 있습니다.", ""]

    for number, pair in enumerate(cmp.pairs, 2):
        out += [f"## {number}. {pair.earlier}년 → {pair.later}년", "", "### 평가지표"]
        if pair.indicators_state == yc.INSUFFICIENT:
            out.append(f"견주지 않았습니다 — {pair.indicators_note}.")
        else:
            counts = pair.counts()
            out.append("같음 {same} · 달라짐 {changed} · 새로 생김 {added} · 보이지 않음 {removed} · 확인 필요 {uncertain}".format(**counts))
            rows = []
            for change in pair.indicators:
                detail = "; ".join(f"{yc.FIELD_LABEL[f]} {before} → {after}" for f, before, after in change.fields)
                rows.append([change.name, yc.STATUS_LABEL[change.status], detail or change.note,
                             notes.mark(change.refs)])
            out += [""] + _table(["지표", "상태", "달라진 칸", "근거"], rows)
            out += ["", "‘보이지 않음’은 그 해 자료에서 찾지 못했다는 뜻이며 폐지를 뜻하지 않습니다."]
        out += ["", "### 처리 절차"]
        if pair.procedure_state == yc.INSUFFICIENT or pair.procedure is None:
            out.append(f"견주지 않았습니다 — {pair.procedure_note or '처리 절차를 읽지 못했습니다'}.")
        else:
            out.append(pair.procedure.headline() + ".")
            for change in pair.procedure.changes:
                refs = cmp.procedures[pair.later][1] if change.kind in ("added", "moved", "shift", "repeat") \
                    else cmp.procedures[pair.earlier][1]
                out.append(f"- {change.sentence} {notes.mark(refs)}")
        out += ["", f"### 시행문에 적힌 항목 ({PAST}, 올해 적용 여부 확인 필요)"]
        if pair.items:
            out += _table(["항목", f"{pair.earlier}년", f"{pair.later}년", "근거"], [
                [row.label, ", ".join(row.values.get(pair.earlier, [])) or "—",
                 ", ".join(row.values.get(pair.later, [])) or "—", notes.mark(row.refs)]
                for row in pair.items])
        else:
            out.append("두 해의 시행문에서 읽은 항목이 없습니다.")
        out.append("")

    out += ["## 부서별 실적 요약 (센 것만 — 인정 여부는 말하지 않습니다)"]
    summaries = [cmp.summaries[y] for y in cmp.years if cmp.summaries[y].rows]
    if summaries:
        out += _table(["연도", "실적 건수", "부서 수", "증빙 칸이 빈 건수", "근거"], [
            [f"{s.year}년", str(s.rows), str(len(s.departments)), str(s.without_proof), notes.mark(s.refs)]
            for s in summaries])
    else:
        out.append("실적표를 읽지 못했거나 실적 문서가 없습니다.")
    out += ["", "## 근거", *(notes.lines() or ["(근거로 쓴 대목이 없습니다)"]), ""]
    return "\n".join(out), notes.refs


def render_evidence(db, found: dict, mat: yc.Materials, cmp: yc.YearComparison) -> str:
    roles = docrole.roles(db, [item["id"] for item in found["included"] + found["candidates"]])
    marks = docyears.years(db, list(roles))
    heads = set(mat.folded.values())

    def row(item: dict) -> list[str]:
        doc_id = item["id"]
        years = ", ".join(f"{docyears.KIND_LABEL[k]} {'·'.join(map(str, v))}"
                          for k, v in marks.get(doc_id, {}).items() if k != docyears.AUTHORED) or "—"
        role = docrole.ROLE_LABEL.get(roles.get(doc_id, {}).get("role", "other"))
        fold = "다른 판의 대표" if doc_id in heads else (f"판 묶음 — {common.filename(db, mat.folded[doc_id])}의 이전 판"
                                                  if doc_id in mat.folded else "")
        return [str(doc_id), common.filename(db, doc_id), role, years, item["why"], fold]

    head = ["문서 번호", "파일", "역할", "해", "찾은 이유", "판 묶음"]
    out = ["# 관련 문서 근거 목록", "",
           f"비교에 쓴 문서 {len(found['included'])}건 · 후보 {len(found['candidates'])}건 · "
           f"의심 {len(found['suspect'])}건 · 읽지 못함 {len(found['unreadable'])}건", "",
           "## 비교에 쓴 문서 (업무에 묶여 있음)", *_table(head, [row(i) for i in found["included"]]), ""]
    for title, key, note in (
            ("업무에 묶이지 않은 후보", "candidates", "비교의 값을 읽지 않았습니다. 이 업무의 자료이면 문서 화면에서 업무에 묶어 주세요."),
            ("의심 — 다른 업무의 자료일 수 있음", "suspect", "업무에 묶여 있으나 비교에서 뺐습니다."),
            ("읽지 못한 문서", "unreadable", "읽을 수 있게 된 뒤 다시 실행하면 반영됩니다."),
            ("제외한 문서", "excluded", "")):
        items = found[key]
        out += [f"## {title}", note, ""] if items else []
        if items:
            out += _table(["문서 번호", "파일", "사유"],
                          [[str(i["id"]), common.filename(db, i["id"]), i["why"]] for i in items]) + [""]
    if mat.undated:
        out += ["## 연도를 정하지 못한 문서", *(f"- {common.filename(db, d)}" for d in mat.undated), ""]
    return "\n".join(out)


def render_questions(db, target: common.Target, cmp: yc.YearComparison, found: dict, mat: yc.Materials,
                     findings: list) -> tuple[str, list]:
    year = target.year
    out = [f"# {year}년 확인 필요사항", "",
           "비교에서 확정하지 못했거나, 올해 기준으로 확인해야 하는 것입니다. 과거 관행과 올해의 공식 의무를 구별해 적었습니다.", "",
           "## 올해 기준"]
    if target.official:
        out.append(f"- {year}년 평가 기준이 확인되었습니다 ({target.how}). 과거와 달라진 점은 보고서의 해당 절을 보세요.")
    else:
        out += [f"- **{year}년 공식 평가계획·지표·시행문을 확보해 주세요.** 지금의 서식과 체크리스트는 과거 자료를 참고한 사전조사용입니다.",
                f"- 평가지표·배점·증빙요건은 {cmp.years[-1] if cmp.years else '과거'}년 기준을 {year}년 기준으로 쓰지 마세요."]
    out.append(f"- 제출기한: {target.due.isoformat() if target.due else '확인 필요'}"
               + (f" ({target.due_note})" if target.due_note else ""))
    items = []
    for pair in cmp.pairs:
        items += [f"- {pair.later}년 지표 ‘{c.name}’: {c.note}" for c in pair.indicators if c.status == yc.UNCERTAIN]
        if pair.indicators_state == yc.INSUFFICIENT:
            items.append(f"- {pair.indicators_note}")
        if pair.procedure_state == yc.INSUFFICIENT:
            items.append(f"- {pair.procedure_note}")
    for year_, names in cmp.conflicts.items():
        items.append(f"- {year_}년 지표 표 안에서 값이 엇갈림: {', '.join(names)}")
    for group in mat.uncertain_groups:
        items.append(f"- 같은 문서의 여러 판 ‘{group['label']}’ 가운데 최신본을 정하지 못했습니다 ({group['latest_reason']})")
    items += [f"- 연도를 정하지 못한 문서: {common.filename(db, d)}" for d in mat.undated]
    if found["candidates"]:
        items.append(f"- 업무에 묶이지 않은 후보 문서 {len(found['candidates'])}건 — 이 업무의 자료인지 확인")
    if found["suspect"]:
        items.append(f"- 다른 업무의 자료일 수 있어 뺀 문서 {len(found['suspect'])}건")
    if found["unreadable"]:
        items.append(f"- 읽지 못한 문서 {len(found['unreadable'])}건")
    out += ["", "## 비교에서 확정하지 못한 것", *(items or ["- 없음"])]
    notes = Notes(db)
    out += ["", "## 지난 평가에서 지적된 것 (과거 지적사항 — 올해도 같은 기준인지 확인 필요)"]
    out += [f"- {sentence} {notes.mark([ref])}" for ref, sentence in findings] or ["- 결과 통보 문서에서 찾지 못했습니다."]
    out += ["", "## 근거", *(notes.lines() or ["(없음)"]), ""]
    return "\n".join(out), notes.refs


def run_compare(context, params: dict) -> dict:
    db = context.db
    found = params["inputs"]["discover"]
    classified = params["inputs"]["classify"]
    name = common.task_name(db, found["task_id"])
    doc_ids = [item["id"] for item in found["included"]]
    mat = yc.materials(db, doc_ids)
    target = common.judge_target(db, context.case_id, doc_ids, classified["target"]["year"], mat)
    cmp = yc.build(db, doc_ids, target.year, include_target=target.official)
    findings = yc.past_findings(db, mat, before=target.year)

    report, report_refs = render_report(db, name, cmp, target, context.today)
    evidence = render_evidence(db, found, mat, cmp)
    questions, question_refs = render_questions(db, target, cmp, found, mat, findings)

    mode = "official" if target.official else "preliminary"
    snapshot = common.sources_of(db, doc_ids)
    save = lambda kind, title, text, refs=(): context.artifacts.save(
        kind, title, f"{target.year}_{name}_{title}.md".replace(" ", "_"), text.encode("utf-8"),
        mode=mode, basis_year=target.year, sources=snapshot, evidence=common.evidence_dicts(refs))
    ids = {"comparison": save("comparison", REPORT_TITLE, report, report_refs),
           "evidence_list": save("evidence_list", EVIDENCE_TITLE, evidence),
           "open_questions": save("open_questions", QUESTIONS_TITLE, questions, question_refs)}
    insufficient = [f"{p.earlier}-{p.later}:{area}" for p in cmp.pairs
                    for area, state in (("indicators", p.indicators_state), ("procedure", p.procedure_state))
                    if state == yc.INSUFFICIENT]
    return {"artifacts": ids, "years": cmp.years, "pairs": len(cmp.pairs),
            "indicator_counts": [p.counts() for p in cmp.pairs], "insufficient": insufficient,
            "findings": len(findings), "mode": mode}


REGISTRY.register(Tool("compare", "1", policy.DRAFT, run_compare, label="연도별 비교"))
