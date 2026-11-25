"""The admin panel's buttons, run for real (the panel's own JavaScript, a tiny fake page, a fake server).

Once, the confirmation window answered "no" to every question, so Block / Reset computer / New key / Delete silently did nothing and nobody
noticed. This test clicks every one of them and checks what was sent to the server and what the admin is told."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
APP = HERE.parent / "bfcloud" / "web" / "app.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_admin_button_asks_first_and_then_does_what_it_promised():
    r = subprocess.run(["node", str(HERE / "panel_harness.js"), str(APP)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "OK", r.stdout + r.stderr
