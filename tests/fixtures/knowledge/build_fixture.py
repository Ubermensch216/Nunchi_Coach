"""P0 합성 기준선 생성 기록. 동결 fixture 변경은 별도 검토와 해시 갱신을 요구한다."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent
TOPICS = ["행정사무감사", "물품구매", "청년지원", "통계보고", "안전점검", "교육신청",
          "복지지원", "주민상담", "주차관리", "건강검진", "시설보수", "보조금정산",
          "민원접수", "재난대응", "장비교체", "사업평가", "성과보고", "예산요구",
          "자산조사", "인력배치", "행사계획", "계약변경", "수요조사", "실적취합"]
CATEGORIES = [("simple", 6, 2), ("alias", 4, 1), ("linked", 5, 2), ("temporal", 5, 2), ("missing", 4, 1)]


def build():
    groups, questions = [], []
    i = 0
    for category, count, tuning in CATEGORIES:
        for j in range(count):
            topic = TOPICS[i]
            key = f"kg{i + 1:02}"
            department = ["복지과", "총무과", "회계과", "기획과"][i % 4]
            org = f"{i + 1}구청"
            docs = []

            def document(suffix, text, year=2026):
                name = f"{key}-{suffix}"
                docs.append({"key": name, "path": f"{key}/{suffix}.txt", "text": text,
                             "sha256": hashlib.sha256(text.encode()).hexdigest(), "document_year": year,
                             "case_year": 2026, "locator": "본문 1"})
                return name

            primary = document("지침", f"{org} {topic} 지침. 담당은 {department}입니다. 제출처는 {org} 기획과입니다. "
                               f"제출기한은 2026-11-{10 + i % 20:02}입니다. {topic} 신청서식을 사용합니다.")
            secondary = None
            if category == "alias":
                secondary = document("별칭", f"{org}에서 {topic}은 '{topic[:2]}업무'로도 부릅니다. "
                                     f"{topic[:2]}업무의 담당은 {org} {department}입니다.")
                document("동명부서", f"다른시청 {department}의 담당 업무는 환경관리입니다.")
            if category == "linked":
                # 서식 안내에는 업무 이름이 없다. 지침의 연결을 따라가야 하는 표본이다.
                docs[0]["text"] += f" 상세 제출 안내는 공통서식-{i + 1} 안내를 참고합니다."
                docs[0]["sha256"] = hashlib.sha256(docs[0]["text"].encode()).hexdigest()
                secondary = document("서식", f"공통서식-{i + 1} 제출 안내. "
                                     f"공통서식-{i + 1}은 {org} 기획과에 제출합니다.")
            if category == "temporal":
                secondary = document("지난지침", f"{org} {topic}의 2025년도 제출기한은 2025-10-15입니다. "
                                     "이것은 전년도 사례이며 올해 적용 여부는 확인되지 않았습니다.", 2025)
                document("변경안내", f"{topic} 제출기한 변경 검토: 2026-12-01. 공식 시행 여부는 미확정입니다.")
            if category == "missing":
                document("참고", f"{topic}의 담당·제출처 자료만 있습니다. 예산·수량·법적 효력·승인·완료 기록은 없습니다.")
            if category == "simple":
                prompts = [f"{topic} 담당은?", f"{topic} 제출처는?", f"{topic} 제출기한은?",
                           f"{topic} 신청서식은?", f"{org} {topic} 지침을 찾아줘"]
            elif category == "alias":
                prompts = [f"{topic[:2]}업무 담당은?", f"{org} {department} 담당 업무는?",
                           f"{topic[:2]}업무와 {topic}은 같은 업무인가?", f"{topic}의 기관은?",
                           f"다른시청 {department}가 {topic}을 담당하나?"]
            elif category == "linked":
                prompts = [f"{topic} 공통서식 제출처는?", f"{topic} 신청서식의 번호는?",
                           f"{topic} 담당과 서식 제출처를 연결해줘", f"{topic} 지침과 서식 원문을 보여줘",
                           f"{topic}을 제출할 서식과 부서는?"]
            elif category == "temporal":
                prompts = [f"{topic} 올해 제출기한은?", f"{topic} 전년도 기한과 올해 기한을 비교해줘",
                           f"{topic} 2026-12-01로 확정되었나?", f"{topic} 기한 변경의 시행 여부는?",
                           f"{topic} 2025년 사례를 올해 사용해도 되나?"]
            else:
                prompts = [f"{topic} 예산 금액은?", f"{topic} 몇 건 완료했나?", f"{topic} 법적 효력이 확정되었나?",
                           f"{topic} 승인자는?", f"{topic} 제출이 완료되었나?"]
            split = "tuning" if j < tuning else "holdout"
            groups.append({"id": key, "category": category, "split": split,
                           "task": topic, "documents": docs})
            for n, prompt in enumerate(prompts):
                expected = [] if category == "missing" else [primary]
                if category in ("alias", "linked"):
                    expected = [primary, secondary]
                elif category == "temporal" and n in (1, 4):
                    expected = [primary, secondary]
                elif category == "temporal" and n in (2, 3):
                    expected = [f"{key}-변경안내"]
                if category == "alias" and n == 4:
                    expected = [f"{key}-동명부서"]
                answers = []
                for doc in docs:
                    if doc["key"] in expected:
                        answers.append({"document": doc["key"], "sha256": doc["sha256"], "locator": doc["locator"],
                                        "exact_quote": doc["text"], "offsets": {"basis": "document_text", "start": 0, "end": len(doc["text"])},
                                        "case_year": 2026})
                questions.append({"id": f"{key}-q{n + 1}", "group": key, "category": category,
                                  "split": split, "question": prompt, "allowed_documents": [d["key"] for d in docs],
                                  "expected_evidence": answers, "allowed_relations": {
                                      "simple": [], "alias": ["OWNED_BY"], "linked": ["REQUIRES_FORM", "SUBMITTED_TO"],
                                      "temporal": ["due_date"], "missing": []}[category],
                                  "answer_policy": "abstain" if category == "missing" else (
                                      "do_not_confirm_effective_change" if category == "temporal" and n >= 2 else "cite")})
            i += 1
    corpus = {"version": 1, "source": "synthetic", "review": "independent_review_pending",
              "basis_date": "2026-10-11", "groups": groups, "questions": questions}
    payload = json.dumps(corpus, ensure_ascii=False, indent=2) + "\n"
    (ROOT / "corpus.json").write_bytes(payload.encode("utf-8"))
    manifest = {"sha256": hashlib.sha256(payload.encode()).hexdigest(), "questions": 120, "tuning": 40, "holdout": 80,
                "top_k": 10, "graph_enabled": False,
                "gates": {"scope_leaks": 0, "unsupported_citations": 0, "lost_decisions": 0,
                          "linked_recall_improvement_pp": 5, "simple_accuracy_max_drop_pp": 2,
                          "graph_p95_ms": 150, "graph_peak_rss_mb": 150},
                "limitations": ["합성 자료", "생성·임베딩 모델 OFF FTS 기준선", "독립 정답 검토 및 실제 비식별 자료 미확보",
                                "P4 전에는 B/C 비교와 답변 정확도·의미 지지 평가 불가"]}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build()
