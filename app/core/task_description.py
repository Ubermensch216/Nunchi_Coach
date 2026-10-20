"""업무 카드에 표시할 자료 기반 개요. 파일 수를 업무 설명으로 쓰지 않는다."""
import re

from . import reconstruction as rc, workstate as ws


def _plain(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def _artifact(value, name):
    """옛 파일명 형태 결과물의 회차·확장자·버전 표기를 걷어 낸다."""
    if not re.search(r'\.(?:hwp[x]?|docx?|xlsx?|csv|pptx?|pdf|txt)\b', value, flags=re.I):
        return _plain(value)
    value = value.split(' — 근거:')[0].split(' (자료에서')[0]
    value = re.sub(r'\.(?:hwp[x]?|docx?|xlsx?|csv|pptx?|pdf|txt)\b', '', value, flags=re.I)
    value = re.sub(r'(^|[_\s])' + re.escape(name) + r'(?=$|[_\s])', r'\1', value)
    value = re.sub(r'(?<!\d)20\d{2}(?:년|년도)?', ' ', value)
    value = re.sub(r'(?:진짜최종|최종|부장수정|수정|송부|복사본)\d*', ' ', value)
    return _plain(value.replace('_', ' ')).strip(' /·-')


def describe(task, card, cycle=None):
    """사용자 설명을 보존하고, 목적·처리 순서·결과물로 짧은 개요를 구성한다."""
    task = dict(task)
    existing = _plain(task.get('description'))
    inventory = bool(re.search(r'문서\s*[\d,]+\s*건|폴더.*자료|본문을 읽지 못한', existing))
    if existing and (task.get('origin') == 'user' or not inventory):
        return existing
    semantic = ' '.join(sentence for sentence in re.split(r'(?<=[.!?])\s+', existing)
                        if not re.search(r'문서\s*[\d,]+\s*건|회차:|폴더 밖|본문을 읽지|이 폴더에는', sentence))
    if semantic:
        return semantic

    # 충돌하는 항목이나 해석만 있는 문장을 확정적인 설명으로 만들지 않는다.
    items = [item for item in card.todo
             if item.get('basis_state') == ws.STATED or item.get('human_state') in ws.CONFIRMED_BY]
    purpose = next((_plain(i['content']) for i in items if i['kind'] == rc.PURPOSE), '')
    outputs = []
    for item in items:
        if item['kind'] == rc.OUTPUT:
            for part in item['content'].split(' / '):
                label = _artifact(part, task['name'])
                if label and label not in outputs:
                    outputs.append(label)
    sentences = []
    if purpose:
        sentences.append(purpose.rstrip('.。') + '.')
    elif card.stated_steps or card.precedent_steps:
        steps = card.stated_steps or card.precedent_steps
        labels = []
        for step in steps:
            label = _plain(re.sub(r'\s*\([^)]*20\d{2}[^)]*\)', '', step['label']))
            if label and label not in labels:
                labels.append(label)
        timing = '매월 ' if cycle and cycle['kind'] == 'monthly' else ''
        prefix = '' if card.stated_steps else '과거 자료에 따르면 '
        sentences.append(prefix + timing + ' → '.join(labels[:3]) + ' 등의 절차를 수행하는 업무입니다.')
    if outputs:
        sentences.append('주요 결과물은 ' + ' · '.join(f'‘{label}’' for label in outputs[:2]) + '입니다.')
    return ' '.join(sentences) or '업무의 목적과 수행 내용을 설명할 근거가 부족합니다. 업무 안내에서 관련 자료를 확인해 주세요.'
