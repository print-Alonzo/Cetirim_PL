"""Build cetirim.pyz: a single self-contained zipapp bundling the whole
scanner -> parser -> semantics -> IR -> VM pipeline, runnable with nothing
but a stdlib Python 3.8+ interpreter (no pip install, no external build
tool) - this is the project's "interpreter software binaries" deliverable.

    python build.py            build cetirim.pyz at the repo root

Then run it exactly like interpreter.py:

    python cetirim.pyz <source_file> [--ir] [--trace] [--symbols] [-O]
    ./cetirim.pyz <source_file>        (Unix - the archive is directly executable)
"""

import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).parent
OUTPUT = ROOT / "cetirim.pyz"

# The modules the pipeline actually needs at runtime - deliberately not
# "every .py file in the repo root", which would also sweep in run_tests.py
# and whatever ad-hoc scripts happen to live there.
MODULES = [
    "scanner.py",
    "ast_nodes.py",
    "grammar_engine.py",
    "grammar.py",
    "parser.py",
    "semantics.py",
    "ir.py",
    "optimizer.py",  # imported lazily by interpreter.py, but only when -O is passed
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
