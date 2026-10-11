"""구조 자격 검사. 원문 존재 검사·의미 지지는 이 결과와 별개다."""
from __future__ import annotations

import re
from datetime import date

from .ontology import load

VALIDATION_VERSION = "1"
DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


def validate_claim(subject_type: str, predicate: str, *, object_type: str | None = None,
                   value=None, value_type: str | None = None,
                   valid_from: str | None = None, valid_to: str | None = None) -> None:
    rule = load()["predicates"].get(predicate)
    if rule is None:
        raise ValueError(f"미등록 관계: {predicate}")
    if subject_type not in rule["domain"]:
        raise ValueError("관계의 출발 유형이 맞지 않습니다.")
    if (object_type is None) == (value is None):
        raise ValueError("개체와 리터럴 값 중 정확히 하나가 필요합니다.")
    if "range" in rule:
        if object_type not in rule["range"] or value_type is not None:
            raise ValueError("관계의 도착 유형이 맞지 않습니다.")
        if rule.get("same_type") and subject_type != object_type:
            raise ValueError("대체 대상은 같은 유형이어야 합니다.")
    elif object_type is not None or value_type != rule["value_type"]:
        raise ValueError("값 유형이 맞지 않습니다.")
    elif value_type == "date":
        _iso_date(value)
    elif value_type == "decimal_amount":
        if (not isinstance(value, dict) or set(value) - {"amount", "currency", "unit"}
                or not isinstance(value.get("amount"), str)
                or not DECIMAL.fullmatch(value["amount"])
                or not any(isinstance(value.get(k), str) and value[k].strip()
                           for k in ("currency", "unit"))
                or any(k in value and (not isinstance(value[k], str) or not value[k].strip())
                       for k in ("currency", "unit"))):
            raise ValueError("금액은 십진 문자열과 통화 또는 단위로 저장해야 합니다.")
    for bound in (valid_from, valid_to):
        if bound is not None:
            _iso_date(bound)
    if valid_from is not None and valid_to is not None and valid_from >= valid_to:
        raise ValueError("적용 기간은 비어 있지 않은 반개구간이어야 합니다.")


def _iso_date(value) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("ISO 날짜가 필요합니다.")
    date.fromisoformat(value)
