"""Work inside the gateway <iframe> embedded in IndiGo's payment page.

The gateway page (payments.juspay.in) is a cross-origin iframe = its own CDP target. We READ its DOM through that
target, convert element positions to top-page coordinates (iframe offset + element offset), and CLICK through
the top page's input pipeline - exactly where a person's mouse would land.
"""
from __future__ import annotations

import json
from typing import Optional

from cdp import CDPBrowser, CDPTab
from human import HumanInput

_FIND = r"""
const re = new RegExp(%s, 'i');
const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
  return r.width >= 4 && r.height >= 4 && st.visibility !== 'hidden' && st.display !== 'none' && +st.opacity !== 0; };
const txt = el => ((el.innerText || el.getAttribute('aria-label') || el.value || '') + '').trim();
const hits = [...document.querySelectorAll('body *')].filter(el => vis(el) && txt(el) && txt(el).length < 140 && re.test(txt(el)));
const inner = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
const out = inner.slice(0, 20).map((el, i) => { const r = el.getBoundingClientRect();
  return {i, tag: el.tagName, text: txt(el).slice(0, 60), x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
          w: Math.round(r.width), h: Math.round(r.height)}; });
return out;
"""

_SCROLL = r"""
const re = new RegExp(%s, 'i');
const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
  return r.width >= 4 && r.height >= 4 && st.visibility !== 'hidden' && st.display !== 'none' && +st.opacity !== 0; };
const txt = el => ((el.innerText || el.getAttribute('aria-label') || el.value || '') + '').trim();
const hits = [...document.querySelectorAll('body *')].filter(el => vis(el) && txt(el) && txt(el).length < 140 && re.test(txt(el)));
const inner = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
const el = inner[%d]; if (!el) return null;
el.scrollIntoView({block: 'center', inline: 'center'});
const r = el.getBoundingClientRect();
return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), text: txt(el).slice(0, 60)};
"""

_FRAME_RECT = r"""
const f = [...document.querySelectorAll('iframe')].find(x => (x.src || '').includes(%s));
if (!f) return null;
const r = f.getBoundingClientRect();
return {x: r.x, y: r.y, w: r.width, h: r.height};
"""


class Frame:
    """A cross-origin iframe target + the top page that hosts it."""

    def __init__(self, top: CDPTab, inner: CDPTab, src_hint: str, human: HumanInput):
        self.top, self.inner, self.src_hint, self.human = top, inner, src_hint, human

    @classmethod
    async def attach(cls, browser: CDPBrowser, top: CDPTab, src_hint: str = "juspay.in") -> Optional["Frame"]:
        inner_info = next((t for t in await browser.targets() if t.get("type") == "iframe" and src_hint in t.get("url", "")), None)
        if not inner_info:
            return None
        inner = await browser.open_tab(inner_info)
        w, h = await top.viewport()
        return cls(top, inner, src_hint, HumanInput(top, w, h))

    async def text(self, limit: int = 900) -> str:
        t = await self.inner.evaluate("document.body ? document.body.innerText : ''")
        return " | ".join(s.strip() for s in (t or "").splitlines() if s.strip())[:limit]

    async def find(self, pattern: str) -> list[dict]:
        return await self.inner.evaluate_json(_FIND % json.dumps(pattern))

    async def click(self, pattern: str, nth: int = 0) -> Optional[dict]:
        pos = await self.inner.evaluate_json(_SCROLL % (json.dumps(pattern), nth))
        if not pos:
            return None
        rect = await self.top.evaluate_json(_FRAME_RECT % json.dumps(self.src_hint))
        if not rect:
            return None
        await self.human.click(rect["x"] + pos["x"], rect["y"] + pos["y"])
        return pos

    async def close(self) -> None:
        await self.inner.close()
