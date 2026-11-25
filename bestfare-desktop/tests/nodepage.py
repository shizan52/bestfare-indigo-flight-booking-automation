"""A stand-in for the booking page that runs the JavaScript the app sends it in Node (in a clean vm context with window/self/location),
so the extension's real core.js can be exercised without a browser."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

RUNNER = r"""
const vm = require('vm'), fs = require('fs');
const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const ctx = {console};
ctx.self = ctx; ctx.window = ctx; ctx.location = {href: 'https://www.goindigo.in/'};
vm.createContext(ctx);
if (job.core) vm.runInContext(job.core, ctx);
Promise.resolve(vm.runInContext('(async () => {' + job.body + '})()', ctx))
  .then(v => process.stdout.write(JSON.stringify(v === undefined ? null : v)))
  .catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(2); });
"""


def have_node() -> bool:
    return shutil.which("node") is not None


class NodePage:
    def __init__(self):
        self.core: str | None = None
        self.calls = 0
        d = Path(tempfile.mkdtemp(prefix="bf_node_"))
        self.runner = d / "runner.js"
        self.runner.write_text(RUNNER, encoding="utf-8")
        self.job = d / "job.json"

    async def evaluate(self, expression: str, await_promise: bool = False, timeout: float = 60.0):
        self.core = expression                              # the app injects the extension's core.js: remember it for later calls
        return None

    async def evaluate_json(self, body_js: str, timeout: float = 60.0):
        self.calls += 1
        self.job.write_text(json.dumps({"core": self.core, "body": body_js}), encoding="utf-8")
        p = subprocess.run(["node", str(self.runner), str(self.job)], capture_output=True, text=True, timeout=60, encoding="utf-8")
        if p.returncode != 0:
            raise RuntimeError(p.stderr[-400:])
        return json.loads(p.stdout) if p.stdout else None


class BrokenPage:
    """A page where the extension rules cannot run at all."""
    async def evaluate(self, *_a, **_k):
        raise RuntimeError("page gone")

    async def evaluate_json(self, *_a, **_k):
        raise RuntimeError("page gone")
