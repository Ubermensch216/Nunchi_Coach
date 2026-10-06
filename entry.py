"""PyInstaller 진입점.

`app/main.py`는 `python -m app.main`으로 실행되는 것을 전제로 상대 임포트를
쓴다. PyInstaller가 그 파일을 직접 분석하면 `app` 패키지 컨텍스트가 없어
"attempted relative import with no known parent package" 오류가 난다.

이 파일은 저장소 루트에 두고 `app`을 정식 패키지로 임포트해 우회한다 —
개발 중 `python -m app.main` 실행 경로는 그대로 두면서, 패키징 시에만
필요한 이 얇은 진입점을 따로 둔다.

`selftest` 서브커맨드
    PyInstaller의 정적 분석이 pymupdf·openpyxl·python-docx 같은 라이브러리의
    동적 임포트를 놓치면, 앱은 뜨지만 실제 파싱에서만 조용히 깨질 수 있다.
    `NunchiCoach.exe selftest <폴더>`로 번들된 실행 파일 그대로 파서를
    실제로 돌려 이 위험을 검증한다.

`licenses` 서브커맨드
    `NunchiCoach.exe licenses`가 오픈소스 고지문을 그대로 내보낸다.
"""

import sys


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "selftest":
        from pathlib import Path
        import app
        root = Path(app.__file__).parent
        required = [root / "ui/assets/logo.png", root / "db/schema.sql",
                    root / "ai/prompts/ask.md", root / "ai/prompts/summarize.md"]
        if getattr(sys, "frozen", False):
            # 고지문 없이 나간 실행물은 배포할 수 없다. 개발 환경에서는 그때그때 만든다.
            required.append(root / "licenses/THIRD_PARTY_NOTICES.txt")
        if any(not path.is_file() for path in required):
            print("Selftest failed: required application resources are missing.", file=sys.stderr)
            return 1
        from app.tools.parse_report import main as parse_report_main
        from app.tools.selfcheck import main as selfcheck_main

        # 문서를 읽는 것만으로는 부족하다 — 업무 복원에서 이어받기까지도 묶음
        # 안에서 실제로 돌려 본다. 둘 다 통과해야 통과다.
        parsed = parse_report_main([*sys.argv[2:], "--strict"])
        return selfcheck_main() or parsed

    if len(sys.argv) >= 2 and sys.argv[1] == "licenses":
        from app.tools import notices

        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(notices.load())
        return 0

    from app.main import main as app_main

    return app_main()


if __name__ == "__main__":
    raise SystemExit(main())
