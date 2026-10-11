"""명시적 업무명·속성 이름표의 값만 대조한다. 문서 우선순위는 추측하지 않는다.

동일 업무·기간의 서로 다른 값을 검색 순위나 파일 수정시각으로 고르지 않는다.
산문·표·주장 의미의 일반적인 충돌 판정은 이 모듈의 범위가 아니다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..core import korean
from . import intent

_LABEL = re.compile(r'^\s*([가-힣A-Za-z][가-힣A-Za-z ·()0-9]{0,39}?)\s*[:：]\s*(\S.*?)\s*$')
_IDENTITY = {'업무명', '사업명', '대상명'}
_UNKNOWN = re.compile(r'^(?:미정|미확정|미기재|정보\s*없음|추후\s*(?:결정|확정|안내)|확인\s*필요)[.!。]*$')
_COMPLEX = re.compile(r'왜|이유|원인|어떻게|비교|차이|증감|합계|총합|평균|가장|각각|전후|그리고')
_AUTHORITY = re.compile(r'현재|최신|최종|확정|유효|개정|변경|이전|이후')
_DATE = re.compile(r'((?:19|20)\d{2})\s*(?:년|[./-])\s*(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})\s*(?:일|[.]?)')
_MONEY = re.compile(r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*(억원|백만원|만원|천원|원)')
_SCALE = {'억원':100000000, '백만원':1000000, '만원':10000, '천원':1000, '원':1}
MAX_DOCUMENTS = 500


def squeeze(text):
    return re.sub(r'\s+', '', text)


def canonical(value):
    """날짜·원화 단위만 동치로 정규화한다. 비교·계산을 위한 답 생성은 하지 않는다."""
    match = _DATE.fullmatch(value.rstrip('.'))
    if match:
        try:
            return ('date', date(*map(int, match.groups())).isoformat())
        except ValueError:
            return ('text', squeeze(value))
    match = _MONEY.fullmatch(value)
    if match:
        return ('won', Decimal(match[1].replace(',','')) * _SCALE[match[2]])
    return ('text', re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', squeeze(value).rstrip('.')))


@dataclass
class Fact:
    doc_id: int
    chunk_id: int
    filename: str
    path: str
    locator: str
    subject: str
    label: str
    value: str
    quote: str
    period: int | None


@dataclass
class Resolution:
    status: str
    facts: list[Fact] = field(default_factory=list)
    documents: int = 0


def resolve(db, asked, scope):
    # 복합 질문·이유 질문에 단일 이름표의 값을 답으로 내놓지 않는다.
    if not asked.field or not asked.stems or _COMPLEX.search(asked.question):
        return None
    label = squeeze(asked.field)
    if len(label) < 2 or scope.doc_ids == []:
        return None
    clause, params = scope.predicate()
    restrictions = [clause, "d.parse_status IN ('ok','partial')"]
    # 흔한 속성명이 다른 모든 업무에 있다는 이유로 범위를 과도하게 보류하지 않는다.
    subjects = [korean.stem(w) for w in asked.subject if len(korean.stem(w))>=3]
    if subjects:
        word = max(subjects,key=len)
        restrictions.append("EXISTS (SELECT 1 FROM chunks s WHERE s.doc_id=d.id AND REPLACE(s.text,' ','') LIKE ? ESCAPE '\\')")
        params.append('%'+word.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
    # FTS 상위권·근접 중복 축약 전에 같은 이름표의 모든 허용 문서를 확인한다.
    escaped = label.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
    restrictions.append("(REPLACE(c.text,' ','') LIKE ? ESCAPE '\\' OR REPLACE(c.text,' ','') LIKE ? ESCAPE '\\')")
    params.extend(['%'+escaped+':%', '%'+escaped+'：%'])
    docs = db.con.execute('SELECT DISTINCT d.id,d.filename,d.path,d.hash,d.eff_year '
        'FROM documents d JOIN chunks c ON c.doc_id=d.id WHERE '+' AND '.join(restrictions)+
        ' ORDER BY d.id LIMIT ?', (*params,MAX_DOCUMENTS+1)).fetchall()
    if len(docs) > MAX_DOCUMENTS:
        return Resolution('scope_too_broad')
    gathered = []
    authority_requested = False
    for doc in docs:
        # 분석된 판본이 실제 원문과 같아야 규칙으로 답한다.
        if not doc['hash']:
            continue
        try:
            with Path(doc['path']).open('rb') as handle:
                if hashlib.file_digest(handle,'sha256').hexdigest() != doc['hash']:
                    continue
        except OSError:
            continue
        rows = db.con.execute('SELECT * FROM chunks WHERE doc_id=? ORDER BY ordinal,id',(doc['id'],)).fetchall()
        labelled = []
        for row in rows:
            clean, _ = intent.strip_instructions(row['text'])
            for line in clean.splitlines():
                match = _LABEL.fullmatch(line)
                if match:
                    labelled.append((row,match[1].strip(),match[2].strip(),line.strip()))
        identities = {squeeze(value):value for _,name,value,_ in labelled if squeeze(name) in _IDENTITY}
        # 업무명이 여럿인 합본, 질문에 업무명이 없는 경우는 의미 해석 경로로 보낸다.
        if len(identities) != 1:
            continue
        identity = next(iter(identities))
        named = (r'(?<![가-힣A-Za-z0-9])' + r'\s*'.join(map(re.escape,identities[identity].split())) +
                 r'(?=$|[\s,.?!]|(?:은|는|이|가|의|에서|에)\s)')
        if len(identity) < 3 or not re.search(named,asked.question):
            continue
        # '보관실 이전' 같은 업무명의 '이전'은 판본 선택 요청이 아니다.
        remainder = squeeze(re.sub(named,'',asked.question)).replace(label,'')
        authority_requested |= bool(_AUTHORITY.search(remainder))
        periods = {int(value) for _,name,value,_ in labelled
                   if squeeze(name)=='기준연도' and re.fullmatch(r'(?:19|20)\d{2}',value)}
        if len(periods)>1:
            continue
        period = next(iter(periods)) if periods else doc['eff_year']
        if scope.years and scope.year_basis == 'document_date' and period not in scope.years:
            continue
        for row,name,value,quote in labelled:
            if squeeze(name) == label:
                gathered.append(Fact(doc['id'],row['id'],doc['filename'],doc['path'],row['locator'],identity,
                                     name,value,quote,period))
    if not gathered:
        return None
    if len({f.subject for f in gathered})>1:
        return Resolution('subject_ambiguous',gathered,len({f.doc_id for f in gathered}))
    # 같은 숫자가 계획/실적 등 다른 이름표에 있으면 그쪽의 값은 모으지 않는다.
    known = [f for f in gathered if not _UNKNOWN.fullmatch(f.value)]
    if len(known) != len(gathered):
        return Resolution('unresolved_field',gathered,len({f.doc_id for f in gathered}))
    distinct = {canonical(f.value) for f in known}
    periods = {f.period for f in known}
    if len(distinct) > 1:
        return Resolution('period_ambiguous' if len(periods)>1 else 'evidence_conflict',known,len({f.doc_id for f in known}))
    # 파일 이름·시각만으로 최종본/현재 효력을 확정하지 않는다.
    if authority_requested:
        return Resolution('authority_unresolved',known,len({f.doc_id for f in known}))
    if len(periods)>1 and not scope.years:
        return Resolution('period_ambiguous',known,len({f.doc_id for f in known}))
    unit = re.search(r'몇\s*(상자|명|건|대|개|곳|장|회|원)',asked.question)
    if unit and any(not re.search(r'\d\s*'+re.escape(unit[1])+r'(?![가-힣])',f.value) for f in known):
        return Resolution('intent_mismatch',known,len({f.doc_id for f in known}))
    if asked.intent in ('amount','count') and not re.search(r'\d',known[0].value):
        return None
    return Resolution('direct_field',known,len({f.doc_id for f in known}))
