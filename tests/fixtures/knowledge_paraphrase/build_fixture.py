"""문체가 다른 합성 평가 묶음 (동결 fixture와 같은 형식, 같은 평가 도구로 읽는다).

동결 120문항은 문서마다 같은 문장 틀이라, 서술문 추출 규칙의 효과가 그 틀에서만 나온 것일
수 있다. 여기서는 그룹마다 문체를 돌려 쓰고 문체별로 따로 잰다. 문체 4(자유 서술)는
규칙이 읽지 못하는 것이 정상이며, 한계를 숨기지 않으려고 일부러 넣었다.
연결 문항은 업무명과 일반어로만 묻는다(둘째 문서의 낱말을 문항에 넣으면 어휘 검색만으로 풀려 연결 효과를 잴 수 없다 — off 기준선 여유만 보고 한 번 고쳤다).
이 묶음의 문장은 추출 규칙을 만든 뒤에 규칙과 다른 틀로 쓴 것이고, 이 묶음을 보고
규칙을 고치지 않는다 — 고치면 이 묶음도 조정용이 된다.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent
TOPICS = ["도로점용허가", "공공시설대관", "체납액징수", "여권발급지원", "하천정비", "어린이집평가",
          "소상공인지원", "전통시장육성", "폐기물처리", "가로등관리", "건축물대장정비", "산림보호",
          "장애인복지", "노인일자리", "청소년수련", "문화행사지원", "공원조성", "상수도점검",
          "환경영향평가", "재난문자발송", "자원봉사관리", "민방위훈련", "지방세감면", "인허가통합"]
DEPARTMENTS = ["복지과", "총무과", "회계과", "기획과", "도로과", "환경과"]
STYLES = {1: "이름표 줄", 2: "다른 이름표의 서술문", 3: "동사형 문장", 4: "자유 서술"}


def primary_text(style, topic, org, department, receiver, deadline_day, form):
    iso = f"2026-11-{deadline_day:02}"
    if style == 1:
        return (f"{org} {topic} 안내\n담당: {department}\n제출처: {receiver}\n"
                f"제출기한: {iso}\n서식: {form}")
    if style == 2:
        return (f"{org} {topic} 안내. 이 업무의 담당 부서는 {department}입니다. 결과 보고처는 {receiver}입니다. "
                f"마감일은 {iso}입니다. 사용 서식은 {form}입니다.")
    if style == 3:
        return (f"{org} {topic} 안내. 이 업무는 {department}에서 맡고 있습니다. 작성한 결과는 {receiver}에 제출합니다. "
                f"2026. 11. {deadline_day}.까지 제출하여 주시기 바랍니다. {form} 양식을 사용합니다.")
    return (f"{org} {topic} 안내. 올해도 이 일은 {department} 소관으로 진행하고 서류는 {receiver} 쪽으로 보내면 된다. "
            f"기한은 11월 {deadline_day}일 전이고 쓰는 양식은 {form}.")


def build():
    groups, questions = [], []
    for i, topic in enumerate(TOPICS):
        style = i % 4 + 1
        key = f"pp{i + 1:02}"
        org = f"{i + 31}구청"
        department = DEPARTMENTS[i % len(DEPARTMENTS)]
        receiver = f"{org} {DEPARTMENTS[(i + 3) % len(DEPARTMENTS)]}"
        form = f"별지 제{i % 9 + 1}호 {topic}신청서"
        alias = f"{topic[:2]}사업"
        docs = []

        def document(suffix, text, year=2026):
            name = f"{key}-{suffix}"
            docs.append({"key": name, "path": f"{key}/{suffix}.txt", "text": text,
                         "sha256": hashlib.sha256(text.encode()).hexdigest(), "document_year": year,
                         "case_year": 2026, "locator": "본문 1"})
            return name

        primary = document("안내", primary_text(style, topic, org, department, receiver, 10 + i % 18, form))
        docs[0]["text"] += f" 서식 작성 방법은 별첨 작성요령-{i + 1}을 참고합니다."
        docs[0]["sha256"] = hashlib.sha256(docs[0]["text"].encode()).hexdigest()
        guide = document("작성요령", f"작성요령-{i + 1} 안내. 작성요령-{i + 1}에 따라 작성한 서류는 {receiver}에 제출합니다."
                         if style != 4 else f"작성요령-{i + 1} 안내. 이 요령으로 만든 서류는 {receiver} 쪽에 내면 된다.")
        nick = document("별칭", f"{org}에서는 {topic}을 '{alias}'이라고도 부릅니다. {alias}의 담당은 {department}입니다."
                        if style != 4 else f"{org}에서는 {topic}을 '{alias}'이라고도 한다. {alias}는 {department}가 한다.")
        document("참고", f"{topic}의 담당·제출처 자료만 있습니다. 예산·수량·승인·완료 기록은 없습니다.")
        groups.append({"id": key, "category": "mixed", "split": "holdout", "style": style,
                       "task": topic, "documents": docs})
        prompts = [("simple", f"{topic} 담당은?", [primary]),
                   ("simple", f"{topic} 제출처는?", [primary]),
                   ("linked", f"{topic} 양식은 어디에 내야 하나?", [primary, guide]),
                   ("linked", f"{topic} 신청서를 접수하는 부서와 쓰는 법은?", [primary, guide]),
                   ("alias", f"{alias} 담당은?", [primary, nick]),
                   ("missing", f"{topic} 예산 금액은?", [])]
        for n, (category, prompt, expected) in enumerate(prompts):
            answers = [{"document": doc["key"], "sha256": doc["sha256"], "locator": doc["locator"],
                        "exact_quote": doc["text"], "offsets": {"basis": "document_text", "start": 0, "end": len(doc["text"])},
                        "case_year": 2026} for doc in docs if doc["key"] in expected]
            questions.append({"id": f"{key}-q{n + 1}", "group": key, "category": category, "split": "holdout",
                              "style": style, "question": prompt, "allowed_documents": [d["key"] for d in docs],
                              "expected_evidence": answers, "allowed_relations": [],
                              "answer_policy": "abstain" if category == "missing" else "cite"})
    corpus = {"version": 1, "source": "synthetic-paraphrase", "review": "independent_review_pending",
              "basis_date": "2026-10-11", "styles": STYLES, "groups": groups, "questions": questions}
    payload = json.dumps(corpus, ensure_ascii=False, indent=2) + "\n"
    (ROOT / "corpus.json").write_bytes(payload.encode("utf-8"))
    manifest = {"sha256": hashlib.sha256(payload.encode()).hexdigest(), "questions": len(questions),
                "tuning": 0, "holdout": len(questions), "top_k": 10, "graph_enabled": False,
                "limitations": ["합성 자료", "추출 규칙을 만든 뒤 다른 문장 틀로 쓴 일반화 확인용",
                                "이 묶음을 보고 규칙을 고치지 않는다", "모델 OFF FTS 기준선"]}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build()
