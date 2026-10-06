from app.search.verify import verify_sentences


def test_display_citation_ids_are_not_treated_as_fact_numbers():
    text = "예산 요구액은 820백만원입니다."
    sources = {1: text, 2: text, 3: text, 4: text}
    result = verify_sentences([{"text": text + " [1, 2, 3, 4]", "sources": [1, 2, 3, 4]}], sources)
    assert result.survived
    assert result.kept[0].text == text


def test_citation_normalization_does_not_hide_unsupported_amount():
    result = verify_sentences([{"text": "예산 요구액은 999백만원입니다. [1]", "sources": [1]}],
                              {1: "예산 요구액은 820백만원입니다."})
    assert not result.survived
    assert "999" in result.dropped[0].reason


def test_inline_citations_must_match_the_structured_sources():
    result = verify_sentences([{"text": "예산 요구액은 820백만원입니다. [2]", "sources": [1]}],
                              {1: "예산 요구액은 820백만원입니다.", 2: "감사 자료"})
    assert not result.survived
