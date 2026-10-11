"""버전 관리된 단일 온톨로지 정의. 실행 환경에 RDF 의존성을 요구하지 않는다."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

ONTOLOGY_PATH = Path(__file__).with_name("ontology.json")


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(ONTOLOGY_PATH.read_text(encoding="utf-8"))


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_label(label: str) -> str:
    # 숫자·기관 구분자·문자 대소문자는 동일성 판단 없이 삭제하지 않는다.
    return " ".join(unicodedata.normalize("NFC", label).split())


def normalize_literal(value, value_type: str | None):
    """동일한 십진 값의 자릿수 표기로 논리 주장을 중복 생성하지 않는다."""
    if value_type != "decimal_amount" or not isinstance(value, dict):
        return value
    result = dict(value)
    # normalize()는 Decimal context 정밀도로 반올림할 수 있어 사용하지 않는다.
    amount = format(Decimal(result["amount"]), "f")
    if "." in amount:
        amount = amount.rstrip("0").rstrip(".")
    result["amount"] = "0" if Decimal(amount).is_zero() else amount
    return result


def claim_key(subject_id: str, predicate: str, *, object_id: str | None = None,
              value=None, value_type: str | None = None, scope_key: str = "",
              valid_from: str | None = None, valid_to: str | None = None,
              conditions: dict | None = None) -> str:
    return fingerprint({"version": load()["claim_key_version"], "subject": subject_id,
                        "predicate": predicate, "object": object_id, "value": normalize_literal(value, value_type),
                        "value_type": value_type, "scope": scope_key,
                        "from": valid_from, "to": valid_to, "conditions": conditions or {}})
