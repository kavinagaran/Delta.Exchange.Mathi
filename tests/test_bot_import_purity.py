"""Importing the bot module must never terminate the interpreter.

Why this file exists: ``Delta_Straddle_Live`` aborted with a module-level
``sys.exit(1)`` when ``API_KEY``/``API_SECRET`` were absent. Four test
modules import it for pure decision logic that never signs a request, and
CI runs deliberately without credentials -- so that exit raised
``SystemExit`` during pytest *collection* and killed the whole run before a
single test executed. The suite is CI's blocking gate, so the gate was down
while merely looking red, which is the exact failure the CI workflow was
written to prevent.

These tests encode the invariant structurally rather than by importing
under a cleared environment: ``load_dotenv()`` resolves relative to the
module's own directory, so a developer's real ``.env`` is found no matter
what the environment or working directory is, and a behavioural check would
silently pass on their machine while CI stayed broken.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import Delta_Straddle_Live as bot

BOT_SOURCE = Path(bot.__file__)


def _module_tree() -> ast.Module:
    return ast.parse(BOT_SOURCE.read_text(encoding="utf-8"))


def _is_sys_exit(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exit"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sys"
    )


def test_importing_the_bot_never_exits_the_interpreter():
    """No ``sys.exit`` may sit on a path that runs at import time.

    Function and class bodies are exempt -- they only run when called.
    Anything left at module scope executes on ``import``, which is what
    took the suite down.
    """
    tree = _module_tree()
    offenders = [
        node.lineno
        for statement in tree.body
        if not isinstance(
            statement,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        )
        for node in ast.walk(statement)
        if _is_sys_exit(node)
    ]
    assert not offenders, (
        "module-level sys.exit() at line(s) "
        f"{offenders} runs during import and aborts pytest collection"
    )


def test_the_bot_still_refuses_to_run_without_credentials():
    """Deferring the guard must not weaken it -- ``main()`` has to check
    before it touches state or the exchange."""
    main_source = inspect.getsource(bot.main)
    first_statement = ast.parse(main_source.strip()).body[0].body[0]
    assert (
        isinstance(first_statement, ast.Expr)
        and isinstance(first_statement.value, ast.Call)
        and getattr(first_statement.value.func, "id", None)
        == "_require_credentials"
    ), "main() must call _require_credentials() before doing anything else"


@pytest.mark.parametrize(
    "key, secret",
    [("", "secret"), ("key", ""), ("", "")],
    ids=["no-key", "no-secret", "neither"],
)
def test_missing_credentials_abort_with_a_nonzero_exit(monkeypatch, key, secret):
    monkeypatch.setattr(bot, "API_KEY", key)
    monkeypatch.setattr(bot, "API_SECRET", secret)
    with pytest.raises(SystemExit) as excinfo:
        bot._require_credentials()
    assert excinfo.value.code == 1


def test_complete_credentials_let_the_bot_start(monkeypatch):
    monkeypatch.setattr(bot, "API_KEY", "key")
    monkeypatch.setattr(bot, "API_SECRET", "secret")
    assert bot._require_credentials() is None
