"""수량 질문에 실제 수량을 답했는가. 단위·원문 대조이며 수량을 추측하지 않는다."""

from __future__ import annotations

import re
from decimal import Decimal

from . import intent

_ALIASES = {'사람':'명','개소':'곳','퍼센트':'%','가지':'종','페이지':'쪽'}
_UNITS = ('세트','상자','개소','사람','퍼센트','페이지','가지','가구','세대','단계',
          '명','건','대','개','곳','장','회','종','병','권','팀','부','쪽','마리','층','호','번','%')
_UNIT = '|'.join(sorted(_UNITS,key=len,reverse=True))
_NUMBER = r'[+-]?\d+(?:,\d{3})*(?:\.\d+)?'
_NATIVE = {'한':1,'하나':1,'두':2,'둘':2,'세':3,'셋':3,'네':4,'넷':4,
           '다섯':5,'여섯':6,'일곱':7,'여덟':8,'아홉':9,'열':10}
_NATIVE_PATTERN = '|'.join(sorted(_NATIVE,key=len,reverse=True))
_EXPLICIT = re.compile(r'(?<![A-Za-z\d,.+\-])(?P<value>'+_NUMBER+r'|(?<![가-힣])(?:'+_NATIVE_PATTERN+r'))\s*(?P<unit>'+_UNIT+
                       r')(?=$|[\s,.!?;：:\[\]|]|[이가은는을를도]|입니다|이다|이며|이고|였다|이상|이하)')
_REQUESTED = re.compile(r'몇\s*('+_UNIT+r')(?=$|[\s?.!,]|이|야|입|였|쯤|정도|인)')
_UNKNOWN = re.compile(r'(?:자료|기록|정보|문서|원문|값|수치|기재|표시|확인).{0,12}(?:없|않|불가|못)|미정|미확인|미기재|알\s*수\s*없')
_NONE = re.compile(r'없(?:다|습니다|음|었)|한\s*(?:건|명|대|개)도.{0,12}않')
_ORDINAL = {'층','호','번'}


def unit_in(question):
    match=_REQUESTED.search(question)
    return _ALIASES.get(match[1],match[1]) if match else ''


def explicit(text, requested=''):
    """연도·날짜는 수량으로 세지 않는다. 번호·층은 그 단위로 물었을 때만 쓴다."""
    found=set()
    for match in _EXPLICIT.finditer(text):
        unit=_ALIASES.get(match['unit'],match['unit'])
        if requested and unit!=requested:
            continue
        if unit in _ORDINAL and unit!=requested:
            continue
        token=match['value']
        value=Decimal(_NATIVE[token]) if token in _NATIVE else Decimal(token.replace(',',''))
        # '한 건도 없었다'는 1건이 아니다. 정보 부재는 0으로 바꾸지 않는다.
        tail=text[match.start():match.end()+24]
        if re.match(r'한\s*(?:건|명|대|개)도',tail):
            if _UNKNOWN.search(tail):
                continue
            if _NONE.search(tail):
                value=Decimal(0)
        found.add((value,unit))
    return found


def _named_scalar(text, asked):
    """이름표·표 칸 또는 '수량은 12입니다'의 단위 없는 값. 날짜/주소는 제외한다."""
    held=set()
    for name,values in intent.bindings(text):
        if any(intent.said_in(stem,name) for stem in asked.stems):
            held.update(value for kind,value,*_ in values if kind=='num')
    squeezed=re.sub(r'\s+','',text)
    for stem in asked.stems:
        pattern=re.escape(re.sub(r'\s+','',stem))+r'(?:은|는|이|가|[:：])(?:총|약)?('+_NUMBER+r')(?=$|[,.!?;]|입니다|이다|이었다|이며|이고)'
        held.update(Decimal(m[1].replace(',','')) for m in re.finditer(pattern,squeezed))
    # typed 값은 explicit()이 단위를 대조한다. 다른 단위의 값을 무단위로 재허용하지 않는다.
    typed={v for v,_ in explicit(text)}
    typed.update(Decimal(m['value'].replace(',','')) for m in _EXPLICIT.finditer(text)
                 if re.fullmatch(_NUMBER,m['value']))
    # 공통 bindings()는 부호를 읽지 않으므로 음수에서 얻은 양수는 되살리지 않는다.
    negative={Decimal(m[0].replace(',','')) for m in re.finditer(r'(?<![\d,.])-\d+(?:,\d{3})*(?:\.\d+)?',text)}
    held.difference_update(abs(value) for value in negative)
    return held-typed


def _explicit_zero(text, asked, requested):
    if not asked.stems:
        return set()
    for clause in re.split(r'[\n;]|\.\s+(?=[가-힣])',text):
        if _NONE.search(clause) and not _UNKNOWN.search(clause) and any(
                intent.said_in(stem,clause) for stem in asked.stems):
            return {(Decimal(0),requested)}
    return set()


def verdict(asked, answer, original):
    """기존 근거/속성 검증 뒤에 호출한다. 답을 만들거나 재생성하지 않는다."""
    if asked.intent!='count':
        return ''
    requested=unit_in(asked.question)
    tail=re.search(r'몇\s*([가-힣A-Za-z%]+)',asked.question)
    if not requested and tail and not re.match(r'^(?:이야|인가|인지|입니|야|정도|이나)',tail[1]):
        # kg·km 등 이 모듈이 아직 읽지 않는 단위는 기존 검증에 맡긴다.
        return ''
    said=explicit(answer,requested)|_explicit_zero(answer,asked,requested)
    supported=explicit(original,requested)|_explicit_zero(original,asked,requested)
    scalar_said=_named_scalar(answer,asked)
    scalar_source=_named_scalar(original,asked)
    # 단위를 생략한 답도 이름표와 원문의 같은 수량에 묶이면 보존한다.
    if scalar_said & (scalar_source|{v for v,_ in supported}):
        return ''
    for value,unit in said:
        if (value,unit) in supported or (value,'') in supported or value in scalar_source:
            return ''
    if said or scalar_said:
        return 'quantity_unsupported'
    if explicit(answer) and requested:
        return 'quantity_unit_mismatch'
    return 'quantity_missing'


_ROLES = ('담당', '참석', '참여', '초청', '수료', '신청', '접수', '계획', '배정')
_BINDING_FILLERS = {'업무', '사업', '자료', '관련', '대한', '대해', '기준', '현재', '전체', '우리'}


def binding_verdict(asked, answer, original, title='', computed_values=None):
    """인원의 역할과 그 숫자가 같은 문장/표 항목에 연결됐는가.

    명시적 역할이 있는 인원 질문에만 적용한다. 이름표·표는 기존 bindings도
    사용하며, 다른 문장의 '담당'이라는 말로 상담 인원을 승인하지 않는다.
    """
    if asked.intent != 'count' or unit_in(asked.question) != '명':
        return ''
    role = next((r for r in _ROLES if r in asked.question), '')
    if not role:
        return ''
    values = explicit(answer, '명') | {(value, '명') for value in _named_scalar(answer, asked)}
    if computed_values is not None:
        values |= {(value, '명') for value in computed_values}
    if not values:
        return ''  # 수량 없음은 기존 verdict가 판정한다.
    prefix = re.split(r'인원|인력|사람|몇', asked.question, maxsplit=1)[0]
    topics = [re.sub(r'(?:에서|으로|의|은|는|이|가|을|를)$', '', w)
              for w in re.findall(r'[가-힣]{2,}', prefix)]
    topics = [w for w in topics if len(w) >= 3 and w not in _BINDING_FILLERS and w not in _ROLES]
    clauses = re.split(r'[\n;]|\.\s+(?=[가-힣])|(?<=[며고])\s+(?=[가-힣])', original)
    # 표의 행·열 이름과 값은 parser/intent가 복원한 묶임으로 대조한다.
    clauses.extend(name + ': ' + ' '.join(str(v[1]) + '명' for v in vals if v[0] == 'num')
                   for name, vals in intent.bindings(original) if re.search(r'인원|인력|명', name))
    for clause in clauses:
        if role not in clause or not (values & explicit(clause, '명')):
            continue
        if not topics or all(word in re.sub(r'\s+', '', title + clause) for word in topics):
            return ''
    return 'quantity_role_unbound'
