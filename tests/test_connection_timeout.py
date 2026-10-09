import asyncio

import httpx
import pytest

from app.ai import client as module


def test_generation_keeps_long_read_timeout_but_bounds_connection(monkeypatch):
    captured=[]
    class Transport:
        def __init__(self,**kwargs):captured.append(kwargs)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kwargs):raise httpx.ConnectError("offline")
    monkeypatch.setattr(module.httpx,"AsyncClient",Transport)
    client=module.OllamaClient()
    with pytest.raises(httpx.ConnectError):
        asyncio.run(client._request_async("POST","/api/generate",timeout=module.GENERATE_TIMEOUT))
    assert captured[0]["timeout"].connect==module.CONNECT_TIMEOUT
    assert captured[0]["timeout"].read==module.GENERATE_TIMEOUT
    assert captured[0]["trust_env"] is False and captured[0]["follow_redirects"] is False
