"""The retired Trend Engine surface must not remain reachable or navigable."""

from pathlib import Path

import dashboard


BASE = Path(dashboard.BASE)


def test_legacy_page_template_and_routes_are_removed():
    rules = {rule.rule for rule in dashboard.app.url_map.iter_rules()}

    assert "trend-engine-legacy" not in dashboard._PAGES
    assert "/trend-engine-legacy" not in rules
    assert "/api/trend-engine" not in rules
    assert not hasattr(dashboard, "api_trend_engine")
    assert not (BASE / "templates" / "trend_engine_legacy.html").exists()


def test_legacy_links_are_removed_from_web_and_android_navigation():
    sources = [
        BASE / "templates" / "base.html",
        BASE / "templates" / "trend_engine.html",
        BASE / "templates" / "dry_run.html",
        BASE / "mv_btc_bot" / "lib" / "main.dart",
    ]

    for path in sources:
        source = path.read_text(encoding="utf-8")
        assert "/trend-engine-legacy" not in source, path
        assert "Trend Engine (Legacy)" not in source, path


def _order_path_reachable() -> set[str]:
    """Every dashboard function reachable from the live score-zone order path.

    Walked from the trading loop's own entry points rather than eyeballed: the
    legacy scorer kept re-entering this path through helpers several calls
    deep, and a grep for `evaluate_trend` cannot tell an order-path caller
    from a display one.
    """
    import ast

    tree = ast.parse((BASE / "dashboard.py").read_text(encoding="utf-8"))
    calls: dict[str, set[str]] = {}
    names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called: set[str] = set()
        referenced: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
            if isinstance(child, ast.Name):
                referenced.add(child.id)
        calls[node.name] = called
        names[node.name] = referenced

    roots = [
        "_trend_auto_loop",
        "_maybe_auto_trend_score_cycle",
        "_maybe_auto_trend_score_live_cycle",
        "_collect_trend_score_auto_signal",
        "_trend_score_auto_live_execute",
    ]
    for root in roots:
        assert root in calls, f"order-path root {root} no longer exists"

    seen: set[str] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for callee in calls.get(current, ()):
            if callee in calls and callee not in seen:
                stack.append(callee)
    _order_path_reachable.names = names  # type: ignore[attr-defined]
    return seen


def test_legacy_scorer_is_unreachable_from_the_live_order_path():
    """The order path must not be able to reach the legacy scorer at all.

    `evaluate_trend` still exists for the legacy preview/dry-run-entry surface,
    so its mere presence in dashboard.py proves nothing. What matters is that
    no function the trading loop can reach calls it.
    """
    reachable = _order_path_reachable()
    calls: dict[str, set[str]] = {}
    import ast

    tree = ast.parse((BASE / "dashboard.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Name):
                        found.add(func.id)
            calls[node.name] = found

    offenders = sorted(
        name for name in reachable
        if {"evaluate_trend", "evaluate_trend_json"} & calls.get(name, set())
    )
    assert offenders == [], (
        "the live order path can reach the legacy scorer via: "
        f"{offenders}. The score must come from btc_trend_engine only."
    )


def test_order_path_does_not_read_the_legacy_default_config():
    """TREND_ENGINE_DEFAULT_CONFIG belongs to the legacy preview surface.

    It was previously pulled into the order path by a
    `_trend_engine_config_overrides()` call whose result nothing read -- a
    discarded value that nonetheless forced the legacy scorer to stay wired
    into live trading.
    """
    reachable = _order_path_reachable()
    names = _order_path_reachable.names  # type: ignore[attr-defined]
    offenders = sorted(
        name for name in reachable
        if "TREND_ENGINE_DEFAULT_CONFIG" in names.get(name, set())
    )
    assert offenders == [], (
        f"order-path functions reading the legacy config: {offenders}"
    )
