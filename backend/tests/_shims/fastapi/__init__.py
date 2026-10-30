"""TEST-ONLY fastapi stand-in exposing only the names the orchestrator import chain needs."""
class WebSocket:
    async def accept(self): pass
    async def send_json(self, data): pass
class WebSocketDisconnect(Exception): pass
