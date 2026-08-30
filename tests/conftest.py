"""
conftest.py — pytest configuration for Heart tests.

Adds the workspace root to sys.path so all modules are importable by their
natural absolute import paths:
    from Brain.src.abuse_filter import ...
    from userdata.src.userdata.ghosts import ...
    from Heart.tools.abuse_bridge import ...
"""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
for _p in [
    _root,                        # Brain/ and userdata/ are packages here
    _root / "Brain" / "src",   # Brain/src/ — for direct imports
    _root / "userdata" / "src", # userdata/src/ — for direct imports
    _root / "Heart" / "tools",  # Heart/tools/ — for abuse_bridge
]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

