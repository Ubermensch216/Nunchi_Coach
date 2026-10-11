"""ontology.json에서 로컬 TTL/SHACL 생성. 개발 전용, 원격 import/JS 미사용.

python -m app.tools.knowledge_standard [--check]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..core.knowledge.ontology import canonical_json, load

OUTPUT = Path(__file__).resolve().parents[1] / "core" / "knowledge" / "standards"


def _classes(names: list[str]) -> str:
    return "sh:or ( " + " ".join(f"[ sh:class kg:{name} ]" for name in names) + " )"


def render() -> dict[str, str]:
    ontology = load()
    prefix = (f'@prefix kg: <{ontology["namespace"]}> .\n'
              '@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n'
              '@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n'
              '@prefix sh: <http://www.w3.org/ns/shacl#> .\n'
              '@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n\n')
    ttl = [prefix, '# Generated from ontology.json. Do not edit.\n']
    ttl += [f'kg:{kind} a rdfs:Class .\n' for kind in ontology["entities"] + ["Claim"]]
    ttl += [f'kg:{name} a rdf:Property ; kg:definition {canonical_json(canonical_json(rule))} .\n'
            for name, rule in ontology["predicates"].items()]
    branches = []
    for name, rule in ontology["predicates"].items():
        branch = (f'[ sh:property [ sh:path kg:predicate ; sh:hasValue kg:{name} ] ; '
                  f'sh:property [ sh:path kg:subject ; {_classes(rule["domain"])} ] ; ')
        if "range" in rule:
            branch += (f'sh:property [ sh:path kg:object ; sh:minCount 1 ; {_classes(rule["range"])} ] ; '
                       'sh:property [ sh:path kg:value ; sh:maxCount 0 ] ; '
                       'sh:property [ sh:path kg:valueType ; sh:maxCount 0 ]')
        else:
            branch += ('sh:property [ sh:path kg:object ; sh:maxCount 0 ] ; '
                       'sh:property [ sh:path kg:value ; sh:minCount 1 ; ')
            if rule["value_type"] == "date":
                branch += 'sh:datatype xsd:date ; sh:pattern "^[0-9]{4}-[0-9]{2}-[0-9]{2}$" ; sh:minInclusive "0001-01-01"^^xsd:date ] ; '
            else:
                branch += ('sh:node [ sh:closed true ; '
                           'sh:property [ sh:path kg:amount ; sh:minCount 1 ; sh:maxCount 1 ; '
                           'sh:datatype xsd:string ; sh:pattern "^-?(0|[1-9][0-9]*)([.][0-9]+)?$" ] ; '
                           'sh:property [ sh:path kg:currency ; sh:maxCount 1 ; sh:datatype xsd:string ; sh:pattern "[^ \\t\\r\\n]" ] ; '
                           'sh:property [ sh:path kg:unit ; sh:maxCount 1 ; sh:datatype xsd:string ; sh:pattern "[^ \\t\\r\\n]" ] ; '
                           'sh:or ( [ sh:property [ sh:path kg:currency ; sh:minCount 1 ] ] '
                           '[ sh:property [ sh:path kg:unit ; sh:minCount 1 ] ] ) ] ] ; ')
            branch += f'sh:property [ sh:path kg:valueType ; sh:hasValue "{rule["value_type"]}" ]'
        branch += ' ]'
        branches.append(branch)
    shacl = prefix + '# Generated from ontology.json. Structure only; not semantic evidence validation.\n'
    shacl += ('kg:ClaimShape a sh:NodeShape ; sh:targetClass kg:Claim ;\n'
              ' sh:property [ sh:path kg:subject ; sh:minCount 1 ; sh:maxCount 1 ] ;\n'
              ' sh:property [ sh:path kg:predicate ; sh:minCount 1 ; sh:maxCount 1 ] ;\n'
              ' sh:property [ sh:path kg:object ; sh:maxCount 1 ] ;\n'
              ' sh:property [ sh:path kg:value ; sh:maxCount 1 ] ;\n'
              ' sh:property [ sh:path kg:valueType ; sh:maxCount 1 ] ;\n'
              ' sh:or (\n ' + '\n '.join(branches) + '\n ) .\n')
    # 같은 유형 제약도 JSON의 same_type에서 생성한다. 조건·기간의 의미는 별도 검사다.
    for name, rule in ontology["predicates"].items():
        if rule.get("same_type"):
            shacl += ('kg:ClaimShape sh:sparql [ sh:message "replacement types differ" ; '
                      f'sh:select """SELECT $this WHERE {{ $this <{ontology["namespace"]}predicate> '
                      f'<{ontology["namespace"]}{name}> ; <{ontology["namespace"]}subject> ?s ; '
                      f'<{ontology["namespace"]}object> ?o . '
                      '?s a ?st . ?o a ?ot . FILTER(?st != ?ot) }""" ] .\n')
    return {"ontology.ttl": "".join(ttl), "shapes.ttl": shacl}


def to_rdf(candidate: dict):
    """개발 fixture 변환. import는 호출 시점에만 한다."""
    from rdflib import BNode, Graph, Literal, Namespace, RDF, XSD

    kg = Namespace(load()["namespace"])
    graph = Graph()
    claim, subject, obj = kg.fixtureClaim, kg.fixtureSubject, kg.fixtureObject
    graph.add((claim, RDF.type, kg.Claim))
    graph.add((claim, kg.subject, subject))
    graph.add((subject, RDF.type, kg[candidate["subject_type"]]))
    graph.add((claim, kg.predicate, kg[candidate["predicate"]]))
    if candidate.get("object_type") is not None:
        graph.add((claim, kg.object, obj))
        graph.add((obj, RDF.type, kg[candidate["object_type"]]))
    value = candidate.get("value")
    if value is not None:
        if isinstance(value, dict):
            node = BNode()
            graph.add((claim, kg.value, node))
            for key, item in value.items():
                graph.add((node, kg[key], Literal(item)))
        else:
            graph.add((claim, kg.value, Literal(value, datatype=XSD.date if candidate.get("value_type") == "date" else None)))
    if candidate.get("value_type") is not None:
        graph.add((claim, kg.valueType, Literal(candidate["value_type"])))
    return graph


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    for name, content in render().items():
        path = OUTPUT / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                print(f"온톨로지 산출물이 다릅니다: {path}")
                return 1
        else:
            OUTPUT.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
