"""Allowlist registry security + resolution contract (scheduler unification T5).

The registry is the ONLY name→callable surface for ``executor_kind ==
"internal"`` jobs. These tests pin: exact-key resolution, the malformed-name
rejection table (separators, ``..``, whitespace, shell metacharacters), the
name-space/import-space isolation (a real import path resolves to nothing
unless literally registered), and the mechanical no-dynamic-resolution guard
mirroring the CI grep gates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import src.scheduling.registry as registry
from src.scheduling.registry import (
    InternalCallableError,
    _INTERNAL_CALLABLES,
    is_registered,
    list_internal,
    register_internal,
    resolve_internal,
)


def _fake_sync() -> None:
    pass


async def _fake_async() -> None:
    pass


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_registered_name_resolves_to_the_same_object(clean_registry) -> None:
    register_internal("test.fake", _fake_sync)
    assert resolve_internal("test.fake") is _fake_sync
    assert is_registered("test.fake")
    assert "test.fake" in list_internal()


def test_sync_and_async_callables_are_both_accepted(clean_registry) -> None:
    register_internal("test.sync", _fake_sync)
    register_internal("test.async", _fake_async)
    assert resolve_internal("test.sync") is _fake_sync
    assert resolve_internal("test.async") is _fake_async


def test_list_internal_is_a_sorted_tuple(clean_registry) -> None:
    register_internal("test.b", _fake_sync)
    register_internal("test.a", _fake_async)
    listed = list_internal()
    assert isinstance(listed, tuple)
    assert listed == tuple(sorted(listed))
    assert {"test.a", "test.b"} <= set(listed)


def test_resolve_is_exact_key_only_no_prefix_or_suffix_match(clean_registry) -> None:
    register_internal("a.b", _fake_sync)
    with pytest.raises(InternalCallableError):
        resolve_internal("a")
    with pytest.raises(InternalCallableError):
        resolve_internal("a.b.c")
    assert not is_registered("a")
    assert not is_registered("a.b.c")


# ---------------------------------------------------------------------------
# Unregistered names
# ---------------------------------------------------------------------------


def test_unregistered_name_error_lists_registered_names(clean_registry) -> None:
    register_internal("test.known", _fake_sync)
    with pytest.raises(InternalCallableError) as excinfo:
        resolve_internal("test.unknown")
    message = str(excinfo.value)
    assert "test.unknown" in message
    assert "not registered" in message
    assert "test.known" in message


def test_error_is_a_key_error_with_readable_str(clean_registry) -> None:
    with pytest.raises(KeyError):
        resolve_internal("test.absent")
    try:
        resolve_internal("test.absent")
    except InternalCallableError as exc:
        # KeyError.__str__ would repr() the message; ours must not be quoted.
        assert str(exc) == exc.args[0]
        assert not str(exc).startswith("'")


@pytest.mark.parametrize(
    "name",
    [
        # Real, dangerous import paths — well-formed dotted identifiers, but
        # NOT registered: the name space is isolated from the import space.
        "os.system",
        "src.br_data.sync.run_sync",
        "shutil.rmtree",
    ],
)
def test_real_import_paths_are_rejected_when_not_registered(clean_registry, name: str) -> None:
    with pytest.raises(InternalCallableError) as excinfo:
        resolve_internal(name)
    assert "not registered" in str(excinfo.value)
    assert not is_registered(name)
    # Isolation proof: rejecting the name never touched the import machinery,
    # so nothing new appeared in the registry either.
    assert name not in _INTERNAL_CALLABLES


# ---------------------------------------------------------------------------
# Malformed-name rejection table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "a:b",
        "a/b",
        "a\\b",
        "a; rm -rf /",
        "a|b",
        "a$(x)",
        "a`x`",
        " a ",
        "",
        "a b",
        "a&&b",
        "a>b",
        "a<b",
        "a(b)",
        "..",
        ".",
        "a..b",
        ".a",
        "a.",
        "a\nb",
        "a\tb",
        "os.system; ls",
        "memory.consolidate --force",
    ],
)
def test_malformed_names_are_rejected_before_any_lookup(clean_registry, name: str) -> None:
    register_internal("test.known", _fake_sync)
    with pytest.raises(InternalCallableError) as excinfo:
        resolve_internal(name)
    assert "rejected" in str(excinfo.value)
    assert not is_registered(name)


@pytest.mark.parametrize("name", [None, 123, b"os.system", ("os", "system")])
def test_non_string_names_are_rejected(clean_registry, name: object) -> None:
    with pytest.raises(InternalCallableError):
        resolve_internal(name)  # type: ignore[arg-type]
    assert not is_registered(name)


def test_is_registered_never_raises(clean_registry) -> None:
    for name in ("", " a ", "a/b", None, 123, "os.system", "test.known"):
        assert isinstance(is_registered(name), bool)


# ---------------------------------------------------------------------------
# register_internal validation (module/test-only surface)
# ---------------------------------------------------------------------------


def test_register_rejects_malformed_names_and_non_callables(clean_registry) -> None:
    with pytest.raises(InternalCallableError):
        register_internal("a/b", _fake_sync)
    with pytest.raises(InternalCallableError):
        register_internal("", _fake_sync)
    with pytest.raises(TypeError):
        register_internal("test.not_callable", "src.memory.consolidate")  # type: ignore[arg-type]
    assert list_internal() == () or "test.not_callable" not in list_internal()


def test_register_rejects_silent_overwrite_without_replace(clean_registry) -> None:
    register_internal("test.dup", _fake_sync)
    with pytest.raises(InternalCallableError):
        register_internal("test.dup", _fake_async)
    assert resolve_internal("test.dup") is _fake_sync
    register_internal("test.dup", _fake_async, replace=True)
    assert resolve_internal("test.dup") is _fake_async


def test_test_registrations_do_not_leak_between_tests() -> None:
    # No clean_registry fixture here: whatever previous tests registered must
    # already be gone (the fixture restores the snapshot on teardown).
    assert not is_registered("test.fake")
    assert not is_registered("test.dup")


# ---------------------------------------------------------------------------
# Mechanical guards (mirror the CI grep gates inside the test suite)
# ---------------------------------------------------------------------------


def test_registry_source_has_no_dynamic_resolution_tokens() -> None:
    source = Path(registry.__file__).read_text(encoding="utf-8")
    for token in ("import_module", "__import__", "eval(", "getattr("):
        assert token not in source, f"registry.py must never contain {token!r}"


def test_registry_has_no_top_level_domain_imports() -> None:
    """The generic scheduling package must not eagerly pull in domain stacks.

    Mechanical pin of the deferred-import contract: every top-level import in
    registry.py is stdlib/typing — the ``src.br_data`` / ``src.memory``
    imports live INSIDE the literal wrapper bodies and run at first firing,
    not at kernel-package import.
    """
    import ast

    tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("src."), f"top-level import {alias.name!r}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module.startswith("src.scheduling") or not module.startswith("src."), (
                f"top-level domain import {module!r}"
            )


def test_registry_source_has_no_shell_tokens() -> None:
    source = Path(registry.__file__).read_text(encoding="utf-8")
    for token in ("subprocess", "os.system", "shell=True"):
        assert token not in source, f"registry.py must never contain {token!r}"


def test_register_internal_is_documented_module_and_test_only() -> None:
    # Invariant guard: runtime string registration is not a production
    # surface — production entries are explicit dict edits at module bottom.
    assert register_internal.__doc__ is not None
    assert "MODULE/TEST-ONLY" in register_internal.__doc__
    assert "module edit" in register_internal.__doc__
