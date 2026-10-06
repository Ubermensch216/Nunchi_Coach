# 라이선스 본문

배포판(wheel)에 본문이 들어 있지 않은 라이선스의 전문을 여기에 둔다.
`app.tools.notices`가 이 폴더의 `*.txt`를 고지문(`THIRD_PARTY_NOTICES.txt`)에 그대로 싣는다.

| 파일 | 받는 곳 | 해당 구성요소 |
|---|---|---|
| `AGPL-3.0.txt` | https://www.gnu.org/licenses/agpl-3.0.txt | PyMuPDF, MuPDF |
| `LGPL-3.0.txt` | https://www.gnu.org/licenses/lgpl-3.0.txt | PySide6, shiboken6, Qt |
| `GPL-3.0.txt` | https://www.gnu.org/licenses/gpl-3.0.txt | LGPL-3.0이 참조하는 본문 |

파일이 없으면 고지문에는 받는 주소만 적히고, `python -m app.tools.notices --strict`가 실패한다.
본문은 고치지 않고 받은 그대로 넣는다.
