"""TEST-ONLY httpx stand-in: import-compatible, performs no network I/O."""
class RequestError(Exception): pass
class TimeoutException(RequestError): pass
class ConnectError(RequestError): pass
class HTTPStatusError(Exception):
    def __init__(self, *a, response=None, **k): super().__init__(*a); self.response = response
class Timeout:
    def __init__(self, *a, **k): pass
class AsyncClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k): raise ConnectError("httpx shim: no network in test environment")
    async def get(self, *a, **k): raise ConnectError("httpx shim: no network in test environment")
