# builds cetirim.pyz, a single-file zipapp of the whole pipeline
# usage: python build.py, then run cetirim.pyz same as interpreter.py

import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).parent
OUTPUT = ROOT / "cetirim.pyz"

# only the modules the pipeline needs at runtime, not everything in the repo root
MODULES = [
    "scanner.py",
    "ast_nodes.py",
    "grammar_engine.py",
    "grammar.py",
    "parser.py",
    "semantics.py",
    "ir.py",
    "optimizer.py",  # only used when -O is passed
    "interpreter.py",
]

MAIN_SHIM = """import interpreter

if __name__ == "__main__":
    interpreter.main()
"""


def build():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        for name in MODULES:
            shutil.copy(ROOT / name, staging / name)
        (staging / "__main__.py").write_text(MAIN_SHIM)
        zipapp.create_archive(staging, target=OUTPUT, interpreter="/usr/bin/env python3")
    OUTPUT.chmod(0o755)
    print(f"Built {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
