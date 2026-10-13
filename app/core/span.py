"""자료가 언제까지의 것인가.

업무마다 '올해 자료가 없습니다'라고 따로 말하면 정작 큰 사실이 안 보인다 — 폴더 전체가
어느 해에서 끊겼는가. 실제 자료는 2023~2024년이 중심이고 그 뒤 스무 달 치가 열 건뿐이었다.
후임자가 가장 먼저 알아야 할 것이 그것이다: 이 폴더가 말해 주지 않는 기간이 있다.
"""

from __future__ import annotations

from datetime import date

MAIN_SHARE = 0.05      # 전체 문서의 이만큼은 있어야 자료가 '있는 해'로 본다


def summary(db, today: date | None = None) -> str:
    """업무 홈 맨 위에 둘 한 줄. 자료가 올해까지 이어지면 빈 문자열이다."""
    today = today or date.today()
    rows = db.con.execute(
        "SELECT eff_year AS year, COUNT(*) AS n FROM documents "
        "WHERE missing_since IS NULL AND eff_year IS NOT NULL AND parse_status != 'skipped' "
        # 파일 날짜뿐인 시점은 복사한 날일 수 있다.
        "AND COALESCE(eff_date_kind, '') != 'fs' GROUP BY eff_year").fetchall()
    counts = {row["year"]: row["n"] for row in rows if row["year"] <= today.year}
    total = sum(counts.values())
    if not total:
        return ""
    main = sorted(year for year, n in counts.items() if n >= max(1, total * MAIN_SHARE))
    first, last = main[0], main[-1]
    if last >= today.year:
        return ""
    later = sum(n for year, n in counts.items() if year > last)
    span = f"{first}년" if first == last else f"{first}~{last}년"
    tail = f"{last + 1}년 이후의 자료는 {later:,}건뿐입니다" if later else f"{last + 1}년 이후의 자료는 없습니다"
    return (f"이 자료는 {span}이 중심이고 {tail}. 그 뒤에 한 일과 바뀐 것은 온나라 문서와 "
            f"현 담당자에게 확인하세요.")
