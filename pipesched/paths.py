from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = ROOT / "configs"
TEMPLATES = CONFIGS / "templates"
SETTINGS = CONFIGS / "settings.yaml"
DATA = ROOT / "data"
GOLD = DATA / "gold"
AUDIT = DATA / "audit"
SHADOW = DATA / "shadow"
COST = DATA / "cost"
EVAL = ROOT / "eval"


def ensure_data_dirs() -> None:
    for p in (GOLD, AUDIT, SHADOW, COST):
        p.mkdir(parents=True, exist_ok=True)
