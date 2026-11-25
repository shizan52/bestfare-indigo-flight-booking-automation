"""Minimal raw Chrome DevTools Protocol client (no Playwright / nodriver).

Why raw CDP: Akamai Bot Manager fingerprints automation frameworks, whose
bootstrap enables CDP domains (Runtime.enable, Page.enable, ...) with detectable
side effects. This client only ever sends the few commands we actually need
(Runtime.evaluate, Input.dispatch*, Network.getCookies/deleteCookies,
Page.captureScreenshot/navigate) and NEVER Runtime.enable.

It attaches to a *human-launched* Edge/Chrome started with
--remote-debugging-port, so the browser itself carries no automation flags.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)


class CDPError(Exception):
    pass


class CDPEvalError(CDPError):
    pass


class CDPTab:
    """One page target controlled over its own DevTools WebSocket."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, info: dict):
        self._ws = ws
        self.info = info
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                fut = self._pending.pop(data.get("id"), None)
                if fut is None or fut.done():
                    continue
                if "error" in data:
                    fut.set_exception(CDPError(str(data["error"])))
                else:
                    fut.set_result(data.get("result", {}))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPError("connection closed"))
            self._pending.clear()

    async def send(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        i = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        await self._ws.send_str(json.dumps({"id": i, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(i, None)
            raise CDPError(f"timeout waiting for {method}")

    # ── evaluation ─────────────────────────────────────────────
    async def evaluate(self, expression: str, *, await_promise: bool = True, timeout: float = 60.0) -> Any:
        res = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            d = res["exceptionDetails"]
            text = (d.get("exception") or {}).get("description") or d.get("text") or "evaluation failed"
            raise CDPEvalError(str(text)[:400])
        return (res.get("result") or {}).get("value")

    async def evaluate_json(self, body_js: str, *, timeout: float = 60.0) -> Any:
        """Run an async JS function body that `return`s a JSON-able value."""
        raw = await self.evaluate(
            "(async () => { const __r = await (async () => {" + body_js + "})(); return JSON.stringify(__r); })()",
            timeout=timeout,
        )
        return json.loads(raw) if isinstance(raw, str) else raw

    # ── cookies ────────────────────────────────────────────────
    async def cookies(self, urls: list[str]) -> list[dict]:
        return (await self.send("Network.getCookies", {"urls": urls})).get("cookies", [])

    async def delete_cookie(self, name: str, *, domain: Optional[str] = None, url: Optional[str] = None) -> None:
        params: dict = {"name": name}
        if domain:
            params["domain"] = domain
        if url:
            params["url"] = url
        await self.send("Network.deleteCookies", params)

    # ── input (isTrusted = true, produced by the browser input pipeline) ──
    async def mouse(self, kind: str, x: float, y: float, *, button: str = "none", buttons: int = 0,
                    click_count: int = 0, delta_x: float = 0, delta_y: float = 0) -> None:
        p = {"type": kind, "x": float(x), "y": float(y), "button": button, "buttons": buttons,
             "clickCount": click_count, "pointerType": "mouse"}
        if kind == "mouseWheel":
            p["deltaX"], p["deltaY"] = float(delta_x), float(delta_y)
        await self.send("Input.dispatchMouseEvent", p, timeout=10)

    async def key(self, kind: str, *, key: str = "", code: str = "", text: str = "", vk: int = 0) -> None:
        p: dict = {"type": kind, "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        if text:
            p["text"] = text
            p["unmodifiedText"] = text
        await self.send("Input.dispatchKeyEvent", p, timeout=10)

    async def viewport(self) -> tuple[int, int]:
        v = await self.evaluate_json("return [innerWidth, innerHeight];")
        return int(v[0]), int(v[1])

    # ── misc ───────────────────────────────────────────────────
    async def screenshot(self, path: str) -> None:
        res = await self.send("Page.captureScreenshot", {"format": "png"}, timeout=30)
        with open(path, "wb") as f:
            f.write(base64.b64decode(res["data"]))

    async def navigate(self, url: str) -> None:
        await self.send("Page.navigate", {"url": url}, timeout=60)

    async def close(self) -> None:
        self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass


class CDPBrowser:
    """Handle on a browser that is already running with --remote-debugging-port."""

    def __init__(self, endpoint: str = "http://127.0.0.1:9222"):
        self.endpoint = endpoint.rstrip("/")
        self._http: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "CDPBrowser":
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http:
            await self._http.close()

    async def version(self) -> dict:
        assert self._http
        async with self._http.get(self.endpoint + "/json/version") as r:
            return await r.json()

    async def pages(self) -> list[dict]:
        assert self._http
        async with self._http.get(self.endpoint + "/json/list") as r:
            return [t for t in await r.json() if t.get("type") == "page"]

    async def targets(self) -> list[dict]:
        """Every debuggable target: pages AND cross-origin iframes (type 'iframe', e.g. the payment gateway)."""
        assert self._http
        async with self._http.get(self.endpoint + "/json/list") as r:
            return await r.json()

    async def open_tab(self, info: dict) -> CDPTab:
        assert self._http
        ws = await self._http.ws_connect(info["webSocketDebuggerUrl"], max_msg_size=0, heartbeat=20)
        return CDPTab(ws, info)

    async def new_page(self, url: str) -> dict:
        """Open `url` in a NEW tab of the same (trusted) profile and return its target info."""
        assert self._http
        async with self._http.put(self.endpoint + "/json/new?" + url) as r:
            return await r.json(content_type=None)

    async def close_page(self, target_id: str) -> None:
        assert self._http
        async with self._http.get(self.endpoint + "/json/close/" + target_id) as r:
            await r.read()

    async def activate_page(self, target_id: str) -> None:
        assert self._http
        async with self._http.get(self.endpoint + "/json/activate/" + target_id) as r:
            await r.read()
