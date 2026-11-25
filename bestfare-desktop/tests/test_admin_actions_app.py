"""Every button of the admin panel, against the REAL app window, over real HTTP, with the real server code (a throw-away copy on this computer).

tools/e2e_admin_app.py makes one temporary user, opens the app window with its key (the engine is a stand-in that can book nothing) and does what an admin does -
Block, Unblock, Edit, Reset computer, Show key, New key, Delete - checking after each step that the window locks (or stays open), that Book does nothing while the key may
not book, that a restart asks for the key again when it has to, and that the activity log says who did what. The same script runs against the production server
(`--released` = with the program the server hands out) - see its header."""
import os
import subprocess
import sys
from pathlib import Path

from livecloud import OWNER_PW, LiveServer

TOOL = Path(__file__).resolve().parents[1] / "tools" / "e2e_admin_app.py"


def test_every_admin_action_reaches_the_running_app(tmp_path):
    srv = LiveServer(tmp_path / "srv")
    try:
        env = dict(os.environ, BESTFARE_ADMIN_USER="boss", BESTFARE_ADMIN_PASS=OWNER_PW, QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, str(TOOL), "--admin", srv.url, "--server", srv.url, "--server-public", srv.public],
                           capture_output=True, text=True, timeout=420, env=env)
    finally:
        srv.stop()
    fails = [ln for ln in r.stdout.splitlines() if ln.startswith("  FAIL")]
    assert r.returncode == 0 and not fails and " 0 failed" in r.stdout, "\n".join(fails) + "\n" + r.stdout[-2500:] + r.stderr[-1500:]
    assert "NOTHING was booked" in r.stdout
