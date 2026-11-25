"""Start Best Fare EXACTLY like the user (`python run.py`), look at the window that really opens, close it like a user, and
check that everything it started is gone again. Windows only.

    python tools\\smoke_run.py OUT_DIR [--demo] [--launcher f:\\indigo\\run.py]

--demo      practice mode (no Edge, no IndiGo). Without it the real app starts - and must NOT start its booking browser (that
            happens only when a fare calendar or a booking needs it); PASS also needs zero app Edge processes after closing.
--launcher  the file to start (default: this folder's run.py; pass the outer f:\\indigo\\run.py to test the easy launcher).
Saves window.png (the real window, drawn by Windows - nothing else on the screen is captured) and prints PASS / FAIL.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32


def windows_titled(fragment: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _l):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(300)
            user32.GetWindowTextW(hwnd, buf, 300)
            if fragment in buf.value:
                found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return found


def window_png(hwnd: int, path: Path) -> tuple[int, int]:
    """PrintWindow the window into a bitmap (only that window's own pixels) and write a PNG."""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    user32.PrintWindow(hwnd, mem, 2)                              # PW_RENDERFULLCONTENT

    class BMI(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    bmi = BMI(ctypes.sizeof(BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, hdc)
    raw = bytearray()
    px = buf.raw
    for y in range(h):
        row = px[y * w * 4:(y + 1) * w * 4]
        rgb = bytearray(w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]          # BGRA -> RGB
        raw += b"\x00" + rgb

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    path.write_bytes(png)
    return w, h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--launcher", default=str(HERE / "run.py"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    python = shutil.which("python") or sys.executable                   # what the user types: python run.py
    cmd = [python, str(Path(a.launcher).resolve())] + (["--demo"] if a.demo else ["--dev"])
    t0 = time.monotonic()
    print(f"starting: {' '.join(cmd)}", flush=True)
    p = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace")
    title = "PRACTICE MODE" if a.demo else "Best Fare — IndiGo bookings"
    win: list = []
    while time.monotonic() - t0 < 120 and not win:
        time.sleep(0.5)
        win = windows_titled(title)
        if p.poll() is not None:
            break
    ok = bool(win)
    print(f"window appeared after {time.monotonic() - t0:.1f}s: {win[:1]}", flush=True)
    port_file = Path(os.environ["LOCALAPPDATA"]) / "BestFare" / "edge-port.txt"
    edge_started = False
    if ok and not a.demo:
        time.sleep(8)                                                    # the window has had time to do anything it would do on its own
        try:                                                             # the booking browser must NOT be running: it starts only when needed
            port = int(port_file.read_text().split()[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                edge_started = b"Edg" in r.read()
        except Exception:  # noqa: BLE001
            edge_started = False
        print(f"booking browser running just after the app opened: {edge_started}   <- must be False", flush=True)
    elif ok:
        time.sleep(2)
    if ok:
        size = window_png(win[0][0], out / "window.png")
        print(f"window.png saved, {size[0]}x{size[1]}", flush=True)
        user32.PostMessageW(win[0][0], 0x0010, 0, 0)                      # WM_CLOSE: the user clicked the X
    try:
        code = p.wait(60)
    except subprocess.TimeoutExpired:
        code = None
        p.kill()
    banner = (p.stdout.read() if p.stdout else "").strip().splitlines()
    print("what run.py printed:", banner[:3], flush=True)
    time.sleep(3)
    left = ""
    if not a.demo:
        ps = ("(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { $_.CommandLine -and "
              "$_.CommandLine.Contains('BestFare\\edge-profile') } | Measure-Object).Count")
        left = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
        print(f"Edge processes of the app still running after closing: {left}", flush=True)
    good = ok and code == 0 and (a.demo or (not edge_started and left == "0"))
    print(f"exit code of run.py: {code}")
    print("RESULT:", "PASS" if good else "FAIL")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
