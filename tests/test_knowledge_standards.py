import json
from pathlib import Path

import pytest

from app.tools.knowledge_standard import OUTPUT, render, to_rdf

CANDIDATES = json.loads((Path(__file__).parent / "fixtures" / "knowledge" / "constraints.json").read_text(encoding="utf-8"))


def test_generated_standards_match_ontology():
    for name, content in render().items():
        assert (OUTPUT / name).read_text(encoding="utf-8") == content


@pytest.mark.parametrize("candidate", CANDIDATES, ids=lambda c: c["id"])
def test_local_shacl_matches_python_sql_fixture(candidate):
    rdflib = pytest.importorskip("rdflib", reason="개발 전용 knowledge 환경에서 실행")
    pyshacl = pytest.importorskip("pyshacl", reason="개발 전용 knowledge 환경에서 실행")
    shapes = rdflib.Graph().parse(data=render()["shapes.ttl"], format="turtle")
    conforms, _, report = pyshacl.validate(to_rdf(candidate), shacl_graph=shapes,
        inference="none", advanced=False, js=False, do_owl_imports=False)
    assert bool(conforms) == candidate["valid"], report
