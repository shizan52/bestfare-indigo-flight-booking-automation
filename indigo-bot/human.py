"""Human-like input for a CDPTab: curved, jittery, eased mouse paths, natural
scrolling, idle wandering and clicks. Events are dispatched through the browser's
own input pipeline (isTrusted=true), so Akamai's behavioural sensor sees organic
signal instead of an idle page."""
from __future__ import annotations

import asyncio
import math
import random

from cdp import CDPTab


def bezier_path(p0: tuple[float, float], p1: tuple[float, float], jitter: float = 0.7) -> list[tuple[float, float]]:
    x0, y0 = p0
    x1, y1 = p1
    dist = math.hypot(x1 - x0, y1 - y0)
    steps = max(10, min(80, int(dist / 9)))
    nx, ny = (-(y1 - y0) / dist, (x1 - x0) / dist) if dist else (0.0, 0.0)
    o1, o2 = random.uniform(-0.28, 0.28) * dist, random.uniform(-0.28, 0.28) * dist
    c1 = (x0 + (x1 - x0) * 0.30 + nx * o1, y0 + (y1 - y0) * 0.30 + ny * o1)
    c2 = (x0 + (x1 - x0) * 0.72 + nx * o2, y0 + (y1 - y0) * 0.72 + ny * o2)
    pts: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)                       # ease-in-out
        u = 1 - t
        x = u ** 3 * x0 + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * x1
        y = u ** 3 * y0 + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * y1
        pts.append((x + random.uniform(-jitter, jitter), y + random.uniform(-jitter, jitter)))
    pts[-1] = (x1, y1)
    return pts


class HumanInput:
    def __init__(self, tab: CDPTab, width: int, height: int):
        self.tab = tab
        self.w, self.h = width, height
        self.pos = (random.uniform(width * 0.25, width * 0.75), random.uniform(height * 0.25, height * 0.7))

    async def move_to(self, x: float, y: float) -> None:
        for px, py in bezier_path(self.pos, (x, y)):
            await self.tab.mouse("mouseMoved", px, py)
            await asyncio.sleep(random.uniform(0.006, 0.019))
        self.pos = (x, y)

    async def click(self, x: float, y: float) -> None:
        await self.move_to(x + random.uniform(-2, 2), y + random.uniform(-2, 2))
        await asyncio.sleep(random.uniform(0.06, 0.18))
        await self.tab.mouse("mousePressed", self.pos[0], self.pos[1], button="left", buttons=1, click_count=1)
        await asyncio.sleep(random.uniform(0.04, 0.12))
        await self.tab.mouse("mouseReleased", self.pos[0], self.pos[1], button="left", buttons=0, click_count=1)

    async def scroll(self, dy: float) -> None:
        sign = 1 if dy >= 0 else -1
        remaining = abs(dy)
        while remaining > 0:
            d = min(remaining, random.uniform(40, 130))
            await self.tab.mouse("mouseWheel", self.pos[0], self.pos[1], delta_y=sign * d)
            remaining -= d
            await asyncio.sleep(random.uniform(0.02, 0.07))

    async def wander(self, seconds: float) -> None:
        """Idle-but-alive behaviour: drifting moves, small scrolls, pauses."""
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            r = random.random()
            if r < 0.62:
                await self.move_to(random.uniform(60, self.w - 60), random.uniform(80, self.h - 80))
            elif r < 0.85:
                await self.scroll(random.choice([-1, 1]) * random.uniform(60, 260))
            await asyncio.sleep(random.uniform(0.25, 1.1))
