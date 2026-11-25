"""Developer tools only: the offers' promo codes for the development gate, read from the bot's config (the workspace has it; a
distribution for users does not). The desktop app itself has no offer codes - with a licence the server hands them out per booking."""
import json
import os
import sys
from pathlib import Path


def load() -> bool:
    if os.environ.get("BESTFARE_DEV_OFFERS"):
        return True
    cfg = Path(__file__).resolve().parents[2] / "indigo-bot"
    if not (cfg / "config.py").exists():
        return False
    sys.path.insert(0, str(cfg))
    import config as botcfg
    os.environ["BESTFARE_DEV_OFFERS"] = json.dumps(dict(botcfg.COUPONS))
    return True
