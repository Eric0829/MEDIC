"""
httpx_mock.py
─────────────────────────────────────────────────────────────────────
httpx 미설치 환경용 mock.

실제 httpx 없이도 MEDIC 파이프라인이 동작하도록 한다.
health check → 항상 503 (서비스 없음으로 감지)
POST → 빈 응답
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations


class _MockResponse:
    def __init__(self, status_code=503, text="mock"):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _MockAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, **kwargs):
        return _MockResponse(503)

    async def post(self, url, **kwargs):
        return _MockResponse(200)


class AsyncClient:
    def __init__(self, **kwargs):
        self._mock = _MockAsyncClient()

    async def __aenter__(self):
        return self._mock

    async def __aexit__(self, *args):
        pass


class ConnectError(Exception):
    pass


class TimeoutException(Exception):
    pass
