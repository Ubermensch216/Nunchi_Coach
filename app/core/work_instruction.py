"""후임자를 위한 설명을 구성한다. 업무 사실과 일반적인 준비 제안을 구분한다.

일정·기관·담당 역할은 분석 항목만 사용한다. 일반 업무 설명은 현재 기관의
확정 절차나 수행 기록으로 저장하지 않고, 매뉴얼의 참고 안내로만 제공한다.
"""
from __future__ import annotations

from dataclasses import replace

from . import reconstruction as rc, task_description, workstate as ws, work_brief


AUDIT_REFERENCE = 'https://law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lspttninfSeq=173505'


def _usable(row):
    return (row.get('basis_state') != ws.CONFLICT
            and (row.get('basis_state') != ws.MISSING or row.get('human_state') in ws.CONFIRMED_BY)
            and row.get('human_state') != ws.RECHECK)


def _value(item, name):
    value = item['content']
    if item['kind'] == rc.OUTPUT:
        value = ' · '.join(task_description._artifact(part, name)
                           for part in value.split(' / '))
    return value


def _facts(items, name, lead):
    return [f"{lead}{_value(item, name)}\n{ws.describe(item['basis_state'], item['applicability'], item['human_state'])}"
            for item in items]


def improve(task, card, sections, cycle=None):
    """이미 모은 근거를 보존하며 설명·산출물·기간·준비 행동을 읽을 순서로 쓴다."""
    task = dict(task)
    by_key = {section.key: section for section in sections}
    audit = task['name'].replace(' ', '') in ('행정사무감사', '행정사무감사준비', '사무감사준비')
    items = [i for i in card.todo + card.prepare if _usable(i)]
    purposes = [i for i in items if i['kind'] == rc.PURPOSE]
    outputs = [i for i in items if i['kind'] == rc.OUTPUT]
    recipients = [i for i in items if i['kind'] == rc.RECIPIENT]
    deadlines = [i for i in items if i['kind'] == rc.DEADLINE]
    scopes = [i for i in items if i['kind'] == rc.SCOPE]

    overview = by_key['overview']
    # 폴더 설명은 업무 정의가 아니다. 수량·회차는 화면 상단의 자료 현황에서 확인한다.
    statements = [i for i in card.found if _usable(i)]
    overview.lines = []
    if audit:
        overview.lines.append(
            '업무 이해를 위한 일반 설명: 행정사무감사는 지방의회가 해당 지방자치단체의 사무 처리 내용을 '
            '살펴보는 업무입니다. 본회의·소관 상임위원회 또는 특별위원회가 감사를 수행하며, '
            '구체적인 대상 기관과 감사할 실적의 기간은 해당 회차의 감사계획에서 확인해야 합니다.')
    description = task_description.describe(task, replace(card, todo=[i for i in card.todo if _usable(i)]))
    if purposes or outputs or card.stated_steps or card.precedent_steps or not audit:
        overview.lines.append(description)
    elif task.get('origin') == 'user' and task.get('description'):
        overview.lines.append(task['description'])
    scope_sentences = {
        '감사주체': '감사를 수행하는 곳은', '감사기관': '감사를 수행하는 곳은',
        '감사대상기관': '감사 대상 기관은', '수감기관': '감사를 받는 기관은',
        '감사대상기간': '실적을 집계할 감사 대상 기간은',
        '감사기간': '실제 감사가 진행되는 기간은',
        '담당업무': '담당자가 맡아 처리할 일은', '업무대상': '이 업무의 대상은',
        '업무수행기간': '담당자가 업무를 수행할 기간은',
        '자료작성기준일': '자료 작성 기준일은',
    }
    overview.lines.extend(
        f"{scope_sentences.get(i['slot'].replace(' ', ''), i['slot'] + '은')} ‘{i['content']}’입니다.\n"
        f"{ws.describe(i['basis_state'], i['applicability'], i['human_state'])}"
        for i in scopes)
    if audit:
        if not any(i['slot'].replace(' ', '') == '담당업무' for i in scopes):
            overview.lines.append(
                '담당 역할을 먼저 구분하세요. 수감기관의 자료 취합·제출 담당은 요구 항목을 부서에 배분하고, '
                '실적과 증빙을 모아 제출자료·질의답변을 준비합니다. 의회 측 담당은 감사계획·자료요구·'
                '감사 진행·결과보고 지원을 맡습니다. 이번 업무가 어느 쪽에 해당하는지는 담당 업무분장과 요청 공문으로 확인하세요.')
        present = {i['slot'].replace(' ', '') for i in scopes}
        missing = [label for label, aliases in (
            ('감사 주체', {'감사주체', '감사기관'}),
            ('대상 기관', {'감사대상기관', '수감기관'}),
            ('감사 대상 기간', {'감사대상기간'}),
            ('담당 역할', {'담당업무'})) if not present & aliases]
        if missing:
            overview.lines.append('이번 회차에서 확인할 범위: ' + ' · '.join(missing)
                                  + '. 감사계획과 업무분장의 해당 항목을 확인해 안내에 보완하세요.')
    elif not scopes:
        overview.lines.append('내 담당 범위는 업무분장과 요청 공문을 대조해 확인하세요. '
                              '직접 작성할 부분, 다른 부서에서 받을 부분, 최종 검토·제출할 담당자를 구분하면 됩니다.')
    overview.lines.extend(i['content'] for i in statements)
    for item in scopes + purposes + outputs + statements:
        overview.evidence.extend(item.get('evidence', []))
    # 충돌·재확인 항목은 설명으로 단정하지 않고 별도로 남긴다.
    overview.lines.extend(f"확인이 필요한 {i['slot']}: {i['content']}\n{ws.describe(i['basis_state'], i['applicability'], i['human_state'])}"
                          for i in card.todo if i['kind'] == rc.SCOPE and not _usable(i))

    output = by_key['output']
    output.lines = _facts(purposes, task['name'], '이 업무의 목적: ')
    output.lines += _facts(outputs, task['name'], '자료에서 확인한 결과물: ')
    output.lines += _facts(recipients, task['name'], '결과물을 전달할 곳: ')
    output.lines += _facts([i for i in card.todo if i['kind'] == rc.AMOUNT], task['name'], '관련 금액: ')
    output.lines += [f"확인이 필요한 {rc.KIND_LABEL.get(i['kind'], i['kind'])}: {i['content']}\n"
                     f"{ws.describe(i['basis_state'], i['applicability'], i['human_state'])}"
                     for i in card.todo if i['kind'] in (rc.PURPOSE, rc.OUTPUT, rc.RECIPIENT) and not _usable(i)]
    if not outputs:
        output.lines.append('최종 결과물은 아직 특정되지 않았습니다. 요청 공문의 제출 항목과 붙임 서식을 '
                            '대조해 작성할 문서·첨부자료 목록을 정하세요.')
    if not recipients:
        output.lines.append('제출처는 아직 확인되지 않았습니다. 요청 공문의 회신처와 내부 취합부서를 확인해 '
                            '내부 검토자 → 취합부서 → 최종 수신처로 이어지는 전달 경로를 정하세요.')
    output.lines.append('완료 확인 제안: 검토·결재를 마친 제출본, 첨부 증빙, 발송·접수 기록을 함께 보관하세요. '
                        '파일을 작성한 것과 실제로 제출한 것을 구분해 다음 담당자가 확인할 수 있어야 합니다.')

    timing = by_key['timing']
    if cycle is not None and cycle['kind'] != 'none':
        period = '매월' if cycle['kind'] == 'monthly' else '해당 월: ' + cycle['months'] + '월'
        basis = '사람이 확인한 반복 일정' if cycle['decided_by'] == 'user' else '과거 자료에서 추정한 반복 일정'
        timing.lines.append(f"{basis}: {period}. {cycle['day_hint'] or ''} "
                            '실제 착수일과 마감일은 이번 회차 요청 공문으로 확인하세요.')
    if audit:
        timing.lines.insert(0, '기간을 세 가지로 나누어 보세요. ① 감사 대상 기간: 실적을 집계할 범위 '
                            '② 감사 실시 기간: 실제 감사가 진행되는 때 ③ 담당자의 준비·제출 기간: '
                            '요청 접수부터 제출·추가답변까지 처리할 때입니다. 폴더의 연도 범위를 감사 대상 기간으로 사용하지 마세요.')
    if not deadlines:
        timing.lines.append('이번 회차의 착수일·내부 취합 마감·최종 제출기한은 아직 확인되지 않았습니다. '
                            '요청 공문과 시행계획에서 먼저 확인한 뒤, 검토·보완 시간을 확보해 부서별 회신기한을 정하세요.')
    timing.lines.append('일정 운영 제안: 요청을 받으면 범위와 서식을 확인하고, 내부 마감까지 자료를 취합·보완한 뒤, '
                        '최종 기한 전에 검토·결재·제출을 마칩니다. 과거 사례의 날짜는 이번 회차 일정과 대조해 사용하세요.')

    first = by_key['first']
    first.lines.insert(0, '인수 직후에는 이번 회차 요청 공문·시행계획과 지난 제출본을 나란히 열어 보세요. '
                       '대상 범위, 작성 기준일, 제출 항목, 서식, 기한, 회신처를 적고 부서별 담당자와 '
                       '현재 접수·미회신·보완 상태를 확인하면 이어서 처리할 일을 정할 수 있습니다.')
    if not by_key['prepare'].lines:
        by_key['prepare'].lines.append('준비 제안: 요청 공문과 붙임 서식, 지난 회차 제출본, 이번 회차 실적·증빙, '
                                      '부서별 담당자 목록을 모으세요. 취합표에는 요청 항목·작성 부서·회신기한·'
                                      '접수 여부·보완사항·최종 반영 여부를 두면 누락을 확인하기 쉽습니다.')
    procedure = by_key['procedure']
    if not card.stated_steps:
        if audit:
            council_role = any(i['slot'].replace(' ', '') == '담당업무' and '의회' in i['content']
                               and '수감' not in i['content'] for i in scopes)
            procedure.lines.append(('의회 측 감사 지원 담당일 때' if council_role else '수감기관 자료 취합 담당일 때')
                                   + '의 준비 순서 제안입니다. 기관에서 확정한 절차는 요청 공문·감사계획으로 확인하세요.')
        else:
            council_role = False
            procedure.lines.append('준비 순서 제안 · 실제 절차는 요청 공문·운영 지침으로 확인')
        stages = work_brief.suggested_stages(audit, council_role)
        steps = [f'{s.when} — {s.action} / 결과물: {s.output}' for s in stages]
        procedure.lines.extend(f'{n}. {text}' for n, text in enumerate(steps, 1))
    if audit:
        by_key['basis'].lines.append('일반 개념 참고: 지방자치법 시행령의 행정사무 감사·조사 규정. '
                                     '현재 기관의 감사계획이나 제출기한을 입증하는 자료는 아닙니다.\n' + AUDIT_REFERENCE)
