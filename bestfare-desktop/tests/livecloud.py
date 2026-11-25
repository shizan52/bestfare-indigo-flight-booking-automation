"""A REAL Best Fare cloud server running in a thread of the test process (real HTTP, real signatures, real database)."""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

CLOUD_DIR = Path(__file__).resolve().parents[2] / "bestfare-cloud"
if str(CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(CLOUD_DIR))

from aiohttp import web  # noqa: E402
from bfcloud import crypto as scrypto  # noqa: E402
from bfcloud.service import Cloud  # noqa: E402
from bfcloud.web import App, Config  # noqa: E402

OWNER_PW = "Correct-Horse-9-Battery"


class LiveServer:
    def __init__(self, data_dir: Path, release_keys=None, secret_identity: Path | None = None, ssl_context=None):
        self.ssl_context = ssl_context
        if secret_identity is not None:                       # (use a given identity: the real one, or one the app does not trust)
            (data_dir).mkdir(parents=True, exist_ok=True)
            (data_dir / "identity.pem").write_bytes(Path(secret_identity).read_bytes())
        self.cloud = Cloud(data_dir, release_keys=release_keys or [])
        self.cloud.create_admin("boss", OWNER_PW, "owner", actor="test")
        self.app = App(self.cloud, Config(data_dir=data_dir))
        self.loop = asyncio.new_event_loop()
        self.port = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(15), "the test server did not start"

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.runner = web.AppRunner(self.app.app)
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=self.ssl_context)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]            # type: ignore[union-attr]
        self._ready.set()
        self.loop.run_forever()

    @property
    def url(self) -> str:
        return f"{'https' if self.ssl_context else 'http'}://127.0.0.1:{self.port}"

    @property
    def public(self) -> str:
        return self.cloud.identity.public_b64

    def stop(self) -> None:
        async def down():
            await self.runner.cleanup()
        asyncio.run_coroutine_threadsafe(down(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        self.cloud.close()
