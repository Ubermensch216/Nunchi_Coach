"""브리지 v1의 경로. 지금은 상태 확인 하나다(연결·꾸러미 경로는 B2·B4에서 더한다)."""
from __future__ import annotations

from collections.abc import Callable
import re

from .. import __version__
from .policy import API_VERSION
from .server import Context, Route


def status_route(describe: Callable[[], dict] = dict) -> Route:
    """`GET /bridge/v1/status`.

    토큰이 없어도 답하지만 그때는 "눈치코치 브리지가 떠 있다"와 호환 판만 알린다.
    열린 프로젝트 같은 상태(`describe`)는 연결된 확장에게만 준다.
    """
    def handler(context: Context):
        payload = {"app": "nunchi-coach", "apiVersion": API_VERSION, "appVersion": __version__,
                   "paired": context.client is not None}
        if context.client is not None:
            payload.update(scopes=list(context.client.scopes), **describe())
        return 200, payload

    return Route("GET", re.compile(r"/bridge/v1/status"), handler, auth="optional")
