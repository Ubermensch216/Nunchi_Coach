# 라이선스 본문

배포판(wheel)에 본문이 들어 있지 않은 라이선스의 전문을 여기에 둔다.
`app.tools.notices`가 이 폴더의 `*.txt`를 고지문(`THIRD_PARTY_NOTICES.txt`)에 그대로 싣는다.

| 파일 | 해당 구성요소 | 원문 |
|---|---|---|
| `AGPL-3.0.txt` | PyMuPDF, MuPDF | https://www.gnu.org/licenses/agpl-3.0.txt |
| `LGPL-3.0.txt` | PySide6, shiboken6, Qt | https://www.gnu.org/licenses/lgpl-3.0.txt |
| `GPL-3.0.txt` | LGPL-3.0이 참조하는 본문 | https://www.gnu.org/licenses/gpl-3.0.txt |

지금 들어 있는 파일은 2026-10-07에 SPDX 라이선스 목록 저장소에서 받았다
(`https://raw.githubusercontent.com/spdx/license-list-data/main/text/{AGPL,LGPL,GPL}-3.0-only.txt`).
그날 이 장비에서 gnu.org에 연결되지 않았기 때문이다. 조문은 같고 줄바꿈 방식이 다르다(문단이 한 줄).
SPDX의 LGPL-3.0 파일은 뒤에 GPL-3.0 본문을 함께 싣고 있다.

파일이 없으면 고지문에는 받는 주소만 적히고, `python -m app.tools.notices --strict`가 실패한다.
본문은 고치지 않고 받은 그대로 넣는다.
