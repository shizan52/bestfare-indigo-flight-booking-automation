"""The windows of the setup program: the install wizard, the update window, and 'Remove Best Fare'. Drawn with update_ui's design; the work itself is setup_core's."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLayout, QLineEdit,
                             QStackedWidget, QVBoxLayout, QWidget)

import setup_core as core
import update_ui as ui
from update_ui import C, MARGIN, Meter, Runner, Steps, Window, button, label, logo, repolish

SIDE_QSS = f"""
QFrame#side {{ background: qlineargradient(x1:0, y1:0, x2:0.6, y2:1, stop:0 {C['top']}, stop:1 {C['bottom']}); border: none;
  border-top-left-radius: 16px; border-bottom-left-radius: 16px; border-top-right-radius: 0; border-bottom-right-radius: 0; }}
QFrame#side QLabel {{ background: transparent; color: white; }}
QLabel#sidebrand {{ font-size: 22pt; font-weight: 700; }}
QLabel#sidetag {{ font-size: 10pt; color: rgba(255,255,255,0.80); }}
QLabel#perk {{ font-size: 10pt; color: rgba(255,255,255,0.92); }}
QLabel#perkdot {{ background: rgba(255,255,255,0.18); color: white; border-radius: 10px; font-weight: 700; font-size: 9pt; }}
QLabel#sidever {{ font-size: 9pt; color: rgba(255,255,255,0.60); }}
QFrame#main {{ background: transparent; border: none; }}
QPushButton#plain {{ background: transparent; color: {C['text2']}; border: none; font-weight: 600; padding: 4px 8px; }}
QPushButton#plain:hover {{ color: {C['primary']}; }}
QLabel#path {{ background: #FBFBFE; border: 1px solid #D7DBEC; border-radius: 10px; padding: 9px 12px; font-size: 9.5pt; color: {C['text']}; }}
"""


def _size_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.0f} MB"


class SetupWindow(QDialog):
    """The wizard's frame: a brand panel on the left, the pages on the right. Frameless, rounded, on a soft shadow; the panel drags it."""

    def __init__(self, version: str):
        super().__init__()
        self.setWindowTitle("Best Fare Setup")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(ui.style() + SIDE_QSS)
        self._drag: Optional[QPoint] = None
        self.closable = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.win = QFrame()
        self.win.setObjectName("win")
        self.win.setFixedSize(800, 500)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(38)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(20, 24, 70, 100))
        self.win.setGraphicsEffect(shadow)
        outer.addWidget(self.win)
        row = QHBoxLayout(self.win)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(270)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(30, 34, 26, 26)
        sv.setSpacing(0)
        mark = QLabel()
        mark.setPixmap(logo(68))
        mark.setFixedSize(68, 68)
        sv.addWidget(mark)
        sv.addSpacing(16)
        b = QLabel("Best Fare")
        b.setObjectName("sidebrand")
        sv.addWidget(b)
        t = QLabel("IndiGo bookings,\nmade calm and quick.")
        t.setObjectName("sidetag")
        sv.addWidget(t)
        sv.addSpacing(30)
        for text in ("Books from your own computer", "Updates itself — always current", "Private to your Windows account"):
            r = QHBoxLayout()
            r.setSpacing(10)
            d = QLabel("✓")
            d.setObjectName("perkdot")
            d.setFixedSize(20, 20)
            d.setAlignment(Qt.AlignmentFlag.AlignCenter)
            p = QLabel(text)
            p.setObjectName("perk")
            p.setWordWrap(True)
            r.addWidget(d, 0, Qt.AlignmentFlag.AlignTop)
            r.addWidget(p, 1)
            sv.addLayout(r)
            sv.addSpacing(12)
        sv.addStretch(1)
        v = QLabel(f"Version {version}")
        v.setObjectName("sidever")
        sv.addWidget(v)
        row.addWidget(side)
        self.side = side

        main = QFrame()
        main.setObjectName("main")
        mv = QVBoxLayout(main)
        mv.setContentsMargins(34, 10, 30, 28)
        mv.setSpacing(0)
        top = QHBoxLayout()
        top.addStretch(1)
        self.close_x = button("✕", "plain")
        self.close_x.clicked.connect(self.reject)
        top.addWidget(self.close_x)
        mv.addLayout(top)
        self.stack = QStackedWidget()
        mv.addWidget(self.stack, 1)
        row.addWidget(main, 1)
        self.center()

    def set_closable(self, yes: bool) -> None:
        self.closable = yes
        self.close_x.setVisible(yes)

    def reject(self) -> None:
        if self.closable:
            super().reject()

    def closeEvent(self, e) -> None:
        if not self.closable:
            e.ignore()
            return
        super().closeEvent(e)

    def center(self) -> None:
        scr = QApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            self.move(g.center().x() - (800 + 2 * MARGIN) // 2, g.center().y() - (500 + 2 * MARGIN) // 2)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and e.position().toPoint().y() < MARGIN + 60 + 40:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        self._drag = None
        super().mouseReleaseEvent(e)


def _page(*items) -> QWidget:
    """A page: widgets and layouts top to bottom; an int is a stretch (empty space that pushes what follows to the bottom)."""
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(12)
    for x in items:
        if isinstance(x, int):
            v.addStretch(x)
        elif isinstance(x, QWidget):
            v.addWidget(x)
        else:
            v.addLayout(x)
    return w


class InstallWizard(SetupWindow):
    """Welcome → Installing → Done (or a plain error with 'Try again')."""
    PAGES = ("welcome", "progress", "done", "error")

    def __init__(self, payload: Path, manifest: dict, existing: Optional[dict]):
        super().__init__(manifest["version"])
        self.payload, self.manifest, self.existing = payload, manifest, existing
        self.version = manifest["version"]
        self.target = Path(existing["dir"]) if existing else core.default_dir()
        self.mode = "install"
        if existing:
            have, now = core.version_number(existing["version"]), core.version_number(self.version)
            self.mode = "update" if have < now else "reinstall" if have == now else "newer"
        self.installed: Optional[dict] = None
        self.runner = Runner()
        self.runner.progressed.connect(self._progress)
        self.runner.phased.connect(self._phase)
        self.runner.finished.connect(self._finished)
        self.pages: dict[str, QWidget] = {}
        self._build_welcome()
        self._build_progress()
        self._build_done()
        self._build_error()
        self.show_page("welcome")

    def show_page(self, name: str) -> None:
        self.stack.setCurrentWidget(self.pages[name])

    def _add(self, name: str, w: QWidget) -> None:
        self.pages[name] = w
        self.stack.addWidget(w)

    # ── welcome ──
    def _build_welcome(self) -> None:
        verb = {"install": "Install", "update": "Update", "reinstall": "Reinstall", "newer": "Best Fare"}[self.mode]
        title = {"install": "Install Best Fare", "update": "Update Best Fare", "reinstall": "Best Fare is already installed",
                 "newer": "A newer Best Fare is already installed"}[self.mode]
        h = label(title, "h1")
        intro = {"install": "Best Fare books IndiGo fares from your own computer. It installs for your Windows account only — you do not need an administrator.",
                 "update": f"Version {self.existing['version'] if self.existing else ''} is installed. This setup brings it to {self.version}. Your history, saved contacts and sign-in stay as they are.",
                 "reinstall": "This is the same version. Reinstalling repairs missing or damaged files; your data stays as it is.",
                 "newer": "This setup is older than the Best Fare on this computer, so it will not install over it."}[self.mode]
        body = label(intro, "body")
        parts: list = [h, body]
        self.folder = QLabel(str(self.target))
        self.folder.setObjectName("path")
        self.folder.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.browse = button("Browse…", "secondary")
        self.browse.clicked.connect(self._choose)
        self.desktop = QCheckBox("Create a desktop shortcut")
        self.desktop.setChecked(True)
        self.startmenu = QCheckBox("Add Best Fare to the Start menu")
        self.startmenu.setChecked(True)
        if self.mode in ("install",):
            parts.append(label("Install location", "small"))
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(self.folder, 1)
            row.addWidget(self.browse)
            parts.append(row)
            parts.append(self.desktop)
            parts.append(self.startmenu)
        else:
            parts.append(label("Installed in  " + str(self.target), "small"))
        self.need = label(f"Needs about {_size_mb(int(self.manifest['size']))} of disk space.", "small")
        parts.append(self.need)
        self.warn = label("", "err")
        parts.append(self.warn)
        parts.append(1)
        row = QHBoxLayout()
        self.cancel = button("Cancel" if self.mode != "newer" else "Close", "secondary")
        self.cancel.clicked.connect(self.reject)
        self.go = button(verb if self.mode != "newer" else "OK", "primary")
        self.go.clicked.connect(self._start if self.mode != "newer" else self.reject)
        if self.mode == "newer":
            self.go.hide()
        row.addStretch(1)
        row.addWidget(self.cancel)
        row.addWidget(self.go)
        parts.append(row)
        self._add("welcome", _page(*parts))

    def _choose(self) -> None:
        start = str(self.target.parent if self.target.parent.exists() else Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose where to install Best Fare", start)
        if chosen:
            p = Path(chosen)
            if p.name.lower() != "best fare":
                p = p / "Best Fare"
            self.target = p
            self.folder.setText(str(p))
            self.warn.setText("")

    # ── progress ──
    def _build_progress(self) -> None:
        self.p_title = label("Installing Best Fare…" if self.mode != "update" else "Updating Best Fare…", "h1")
        self.p_text = label("This takes about a minute. Please keep this window open.", "body")
        self.steps = Steps(["Preparing", "Copying files", "Shortcuts", "Finishing"])
        self.meter = Meter()
        self.detail = label("", "small", wrap=False)
        self._add("progress", _page(self.p_title, self.p_text, 1, self.steps, self.meter, self.detail, 1))

    # ── done / error ──
    def _build_done(self) -> None:
        tick = QLabel("✓")
        tick.setObjectName("bigcheck")
        tick.setFixedSize(68, 68)
        tick.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.d_title = label("Best Fare is ready", "h1")
        self.d_text = label("", "body")
        self.open_now = QCheckBox("Open Best Fare now")
        self.open_now.setChecked(True)
        self.finish = button("Finish", "primary")
        self.finish.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.finish)
        self._add("done", _page(1, tick, self.d_title, self.d_text, self.open_now, 1, row))

    def _build_error(self) -> None:
        cross = QLabel("!")
        cross.setObjectName("bigbad")
        cross.setFixedSize(68, 68)
        cross.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.e_title = label("Best Fare could not be installed", "h1")
        self.e_text = label("", "body")
        self.e_close = button("Close", "secondary")
        self.e_close.clicked.connect(self.reject)
        self.e_retry = button("Try again", "primary")
        self.e_retry.clicked.connect(self._start)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.e_close)
        row.addWidget(self.e_retry)
        self._add("error", _page(1, cross, self.e_title, self.e_text, 1, row))

    # ── doing it ──
    def _start(self) -> None:
        self.warn.setText("")
        self.show_page("progress")
        self.set_closable(False)
        self.steps.set(0)
        self.meter.indeterminate("")
        self.detail.setText("")
        update = self.mode in ("update", "reinstall")
        desktop, startmenu = self.desktop.isChecked(), self.startmenu.isChecked()
        target = self.target

        def job(progress):
            def say(what: str, done: int, total: int) -> None:
                progress.phase(what)
                progress(done, total)
            return core.install(self.payload, target, desktop=desktop, startmenu=startmenu, update=update, progress=say)
        self.runner.run(job)

    def _phase(self, what: str) -> None:
        if what == "Preparing":
            self.steps.set(0)
        elif what == "Shortcuts":
            self.steps.set(2)
        elif what == "Done":
            self.steps.set(3)
        else:
            self.steps.set(1)
            self.detail.setText(what if len(what) < 60 else "…" + what[-58:])

    def _progress(self, done: int, total: int) -> None:
        if self.steps.dots[1].property("dot") == "active":
            self.meter.set(done, total)

    def _finished(self, result, error) -> None:
        self.set_closable(True)
        if error is not None:
            core.log("install failed: " + repr(error))
            self.e_text.setText(error.args[0] if isinstance(error, core.SetupError) and error.args else "Something went wrong during the installation. Please try again.")
            self.show_page("error")
            return
        self.installed = result
        problems = result.get("shortcut_problems") or []
        done_text = (f"Version {result['version']} is installed in {result['dir']}." + ("\nA shortcut could not be created — you can still open Best Fare from the folder." if problems else ""))
        self.d_text.setText(done_text)
        self.show_page("done")


def run_install(payload: Path, manifest: dict, existing: Optional[dict]) -> int:
    """The wizard. Returns 0 when installed, 1 when the user left without installing."""
    w = InstallWizard(payload, manifest, existing)
    ok = w.exec() == QDialog.DialogCode.Accepted and w.installed is not None
    if ok and w.open_now.isChecked():
        try:
            core.launch(w.installed["dir"])
        except OSError:
            pass
    return 0 if ok else 1


# ── the update window of the setup (the installed app started it and closed itself) ───────────────────────────────────
def run_update(payload: Path, manifest: dict, existing: Optional[dict], wait_pid: Optional[int], target_arg: Optional[Path]) -> int:
    target = Path(target_arg) if target_arg else (Path(existing["dir"]) if existing else core.default_dir())
    current = existing["version"] if existing else ""
    dlg = ui.UpdateDialog(title="Updating Best Fare", tagline="Installing the new version", current=current, new=manifest["version"],
                          notes=str(manifest.get("notes") or ""), steps=("Closing", "Installing", "Starting"), mandatory=True)
    state = {"ok": False}

    def job(progress):
        progress.phase("Closing")
        if wait_pid:
            core.wait_for_exit(wait_pid, 60.0)
        for _ in range(60):                                      # (the program's files are released a moment after the program ends)
            if not core.app_running(target):
                break
            time.sleep(0.5)
        progress.phase("Installing")

        def say(what: str, done: int, total: int) -> None:
            if what not in ("Preparing", "Shortcuts", "Done"):
                progress(done, total)
        core.install(payload, target, update=True, progress=say)
        progress.phase("Starting")
        core.launch(target)
        return True

    outcome, err = ui.run_update(dlg, job, ask=None, working_text="Please wait — this takes a moment.", done_text=f"Best Fare {manifest['version']} is ready.",
                                 close_after_ms=1100, phases={"Closing": 0, "Installing": 1, "Starting": 2})
    state["ok"] = outcome == "ok"
    if not state["ok"]:
        core.log(f"update failed: {err!r}")
        try:                                                         # the old version is still there: start it so the user is not left with nothing
            if (target / core.EXE).is_file():
                core.launch(target)
        except OSError:
            pass
        return 1
    return 0


# ── removing ───────────────────────────────────────────────────────────────────────────────────────────────────────
class UninstallDialog(Window):
    def __init__(self, target: Path, version: str):
        super().__init__("Best Fare", "Remove from this computer", width=520)
        self.target = target
        self.title = label("Remove Best Fare?", "h1")
        self.body.addWidget(self.title)
        self.text = label(f"Best Fare {version} will be removed from this computer, with its shortcuts.", "body")
        self.body.addWidget(self.text)
        self.purge = QCheckBox("Also delete my data: booking history, saved contacts and sign-in")
        self.body.addWidget(self.purge)
        self.hint = label("Keep this unticked if you may install Best Fare again — everything will still be there.", "small")
        self.body.addWidget(self.hint)
        self.meter = Meter()
        self.meter.hide()
        self.body.addWidget(self.meter)
        self.problem = label("", "err")
        self.problem.hide()
        self.body.addWidget(self.problem)
        row = QHBoxLayout()
        self.cancel = button("Cancel", "secondary")
        self.cancel.clicked.connect(self.reject)
        self.go = button("Remove", "primary")
        row.addStretch(1)
        row.addWidget(self.cancel)
        row.addWidget(self.go)
        self.body.addLayout(row)
        self.runner = Runner()
        self.go.clicked.connect(self._remove)
        self.runner.finished.connect(self._finished)
        self.removed = False
        self.center()

    def _remove(self) -> None:
        self.go.setEnabled(False)
        self.cancel.setEnabled(False)
        self.purge.setEnabled(False)
        self.problem.hide()
        self.meter.show()
        self.meter.indeterminate("Removing…")
        self.set_closable(False)
        purge, target = self.purge.isChecked(), self.target
        running_from = _inside(Path(sys.executable), target)
        self.runner.run(lambda progress: core.uninstall(target, purge_data=purge, running_from_target=running_from))

    def _finished(self, _result, error) -> None:
        self.set_closable(True)
        if error is not None:
            core.log("uninstall failed: " + repr(error))
            self.problem.setText(error.args[0] if isinstance(error, core.SetupError) and error.args else "Best Fare could not be removed. Please try again.")
            self.problem.show()
            self.meter.hide()
            self.go.setEnabled(True)
            self.go.setText("Try again")
            self.cancel.setEnabled(True)
            self.purge.setEnabled(True)
            return
        self.removed = True
        self.title.setText("Best Fare was removed")
        self.text.setText("Thank you for using Best Fare." + (" Your data was deleted too." if self.purge.isChecked() else " Your data was kept on this computer."))
        self.hint.hide()
        self.purge.hide()
        self.meter.hide()
        self.cancel.hide()
        self.go.setText("Close")
        self.go.setEnabled(True)
        self.go.clicked.disconnect()
        self.go.clicked.connect(self.accept)


def _inside(path: Path, folder: Path) -> bool:
    try:
        p, f = path.resolve(), folder.resolve()
    except OSError:
        return False
    return p == f or f in p.parents


def run_uninstall(target: Path, version: str) -> int:
    d = UninstallDialog(target, version)
    d.exec()
    return 0 if d.removed else 1
