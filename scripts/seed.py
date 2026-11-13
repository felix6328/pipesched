#!/usr/bin/env python3
"""把合成项目写入本地 Kitsu。全部为模拟数据。"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipesched.seed import SEED_DEFAULT, run_seed

if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else SEED_DEFAULT
    run_seed(seed=seed)
