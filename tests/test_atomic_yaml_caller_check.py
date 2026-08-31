"""
test_atomic_yaml_caller_check.py — Static check for _atomic_write_yaml callers.

Walks the AST of heart.py and asserts every call to _atomic_write_yaml that
passes a list (potential multi-doc) explicitly passes multi_doc=True. Catches
the "forgot multi_doc=True" footgun at lint time so it can't regress.

Run: python -m pytest Heart/tests/test_atomic_yaml_caller_check.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
HEART_PY = ROOT / "Heart" / "tools" / "heart.py"


def _find_atomic_write_yaml_calls() -> list[ast.Call]:
    tree = ast.parse(HEART_PY.read_text(encoding="utf-8"))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_atomic_write_yaml":
            calls.append(node)
    return calls


def _has_multi_doc_kwarg(call: ast.Call) -> bool:
    return any(kw.arg == "multi_doc" for kw in call.keywords)


def test_atomic_write_yaml_caller_count_in_heart_py():
    """Sanity: heart.py should have at least one call to _atomic_write_yaml.
    If a refactor removes all callers, this test fails and the engineer
    should also check whether the helper is still needed."""
    calls = _find_atomic_write_yaml_calls()
    assert len(calls) >= 1, "no _atomic_write_yaml callers found in heart.py; was the helper removed?"


def test_every_atomic_write_yaml_caller_passes_multi_doc_or_singleton():
    """For every _atomic_write_yaml call in heart.py, if the first positional
    arg is a list (AST List node), the call MUST pass multi_doc=True. Singletons
    (dict, str) are not subject to this rule since they always write a single doc."""
    calls = _find_atomic_write_yaml_calls()
    offenders: list[str] = []
    for call in calls:
        if not call.args:
            continue
        first_arg = call.args[0]
        # If the first argument is a list literal (AST.List), it's potentially multi-doc.
        if isinstance(first_arg, ast.List):
            if not _has_multi_doc_kwarg(call):
                line_no = call.lineno
                offenders.append(
                    f"_atomic_write_yaml call at line {line_no} passes a list as first arg "
                    f"but does not pass multi_doc=True (footgun: list will be flattened to "
                    f"a single YAML document by yaml.dump)"
                )
    assert not offenders, "\n".join(offenders)


def test_multi_doc_true_call_writes_multiple_documents(tmp_path):
    """End-to-end: write 3 separate YAML docs via _atomic_write_yaml(multi_doc=True)
    and read them back to confirm '---' separation is preserved. This is the
    round-trip guarantee the static check defends."""
    import importlib
    import sys
    for k in list(sys.modules.keys()):
        if "heart" in k:
            del sys.modules[k]
    sys.path.insert(0, str(ROOT / "Heart" / "tools"))
    import yaml
    import heart as _heart_module

    path = tmp_path / "multi.yaml"
    docs = [{"a": 1}, {"a": 2}, {"a": 3}]
    _heart_module._atomic_write_yaml(path, docs, multi_doc=True)
    content = path.read_text(encoding="utf-8")
    assert content.count("---") >= 2, f"expected at least 2 doc separators (yaml.dump_all omits leading ---), got: {content!r}"
    parsed = list(yaml.safe_load_all(content))
    assert len(parsed) == 3
    assert [d["a"] for d in parsed] == [1, 2, 3]
