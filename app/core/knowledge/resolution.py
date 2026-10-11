"""사람 결정을 재생한다. 동일성 병합은 P3/P5의 비파괴 view에서 적용한다."""


def latest_decision(rows):
    cancelled = {row["supersedes_decision"] for row in rows if row["decision"] == "undo"}
    return next((row for row in reversed(rows) if row["uuid"] not in cancelled and row["decision"] != "undo"), None)
