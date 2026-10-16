"""Allowlist registry for internal scheduled callables (scheduler unification).

The ONLY name→callable mapping available to ``executor_kind == "internal"``
jobs. Security contract (plan D4, user-confirmed — this is the safety
bedrock, "no arbitrary shell, no dynamic import"):

* Keys are literal strings registered in THIS module. The production surface
  is the explicit ``_INTERNAL_CALLABLES`` dict at the bottom, populated by
  module edit only. Nothing in a request path (HTTP body, job file free
  text, environment variable) may ever register a callable;
  :func:`register_internal` exists for module edits and tests.
* Resolution is an exact-key dict lookup. There is NO dynamic resolution of
  any kind: names are never parsed as import paths, never turned into code,
  and never walked through attribute chains. A name that happens to be a
  real import path (a stdlib process-spawning function, say) resolves to
  nothing unless that exact string is a registered key — the registry's
  name space and Python's import space are fully isolated.
* Malformed names are rejected before any lookup: the shape allowlist
  (dotted identifiers only) fails every separator, ``..``, whitespace and
  shell-metacharacter form by construction.
* Values are callables taking no required arguments, sync or async. The run
  contract lives on the executor side (see
  :mod:`src.scheduled_research.internal_dispatch`): sync callables are
  offloaded through ``asyncio.to_thread``, async callables are awaited
  directly, always in a background task — never on the serialized tick loop.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable

#: A registered internal callable: invoked with no arguments, either sync
#: (return value discarded) or async. ``...`` tolerates defaulted/optional
#: parameters; the run contract above is what the executor guarantees.
InternalRunnable = Callable[..., Awaitable[None] | None]

#: Dotted-identifier shape allowlist, e.g. ``memory.consolidate``. Being an
#: allowlist (not a denylist) is the point: path separators, ``..``, ``:``,
#: whitespace and shell metacharacters (``; | & $ < > ( )`` and backtick)
#: fail the shape check by construction and never reach the dict.
_VALID_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


class InternalCallableError(KeyError):
    """A name cannot be resolved to a registered internal callable.

    Subclasses :class:`KeyError` so consumers may catch either; ``__str__``
    is overridden because :class:`KeyError` would otherwise ``repr()`` the
    message and quote it.
    """

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


def _check_name(name: object) -> str:
    """Return *name* if it has the dotted-identifier shape, else reject it."""
    if not isinstance(name, str) or not _VALID_NAME_RE.match(name):
        raise InternalCallableError(
            f"internal callable name {name!r} is rejected: only dotted identifiers "
            "(letters, digits, '_', single '.' separators) are allowed"
        )
    return name


def register_internal(name: str, fn: InternalRunnable, *, replace: bool = False) -> None:
    """Register *fn* under the literal key *name*.

    MODULE/TEST-ONLY: the production surface is the explicit dict entries at
    the bottom of this module, added by module edit (scheduler-unification
    T8/T9). No runtime input may reach this function — the invariant is
    documented here and guarded by ``test_registry.py``.

    Args:
        name: Literal registry key (dotted-identifier shape).
        fn: Zero-required-arg callable, sync or async.
        replace: Allow overwriting an existing key. Off by default so a
            production entry is never silently clobbered.

    Raises:
        InternalCallableError: *name* is malformed, or already registered
            without ``replace=True``.
        TypeError: *fn* is not callable.
    """
    key = _check_name(name)
    if not callable(fn):
        raise TypeError(f"internal callable {key!r} requires a callable, got {type(fn).__name__}")
    if not replace and key in _INTERNAL_CALLABLES:
        raise InternalCallableError(f"internal callable {key!r} is already registered")
    _INTERNAL_CALLABLES[key] = fn


def resolve_internal(name: str) -> InternalRunnable:
    """Return the callable registered under the EXACT key *name*.

    Exact-key dict lookup only — never a prefix match, never a parse, never
    a fallback to Python's import machinery.

    Args:
        name: Registry key from a job's ``internal_callable`` field.

    Returns:
        The registered callable object (identity preserved).

    Raises:
        InternalCallableError: *name* is malformed (rejected before lookup)
            or is not a registered key; the message for an unregistered
            well-formed name lists every registered name.
    """
    key = _check_name(name)
    try:
        return _INTERNAL_CALLABLES[key]
    except KeyError:
        raise InternalCallableError(
            f"internal callable {key!r} is not registered; registered names: {list_internal()}"
        ) from None


def list_internal() -> tuple[str, ...]:
    """Return every registered key, sorted."""
    return tuple(sorted(_INTERNAL_CALLABLES))


def is_registered(name: object) -> bool:
    """Whether *name* is a well-formed AND registered key. Never raises."""
    return isinstance(name, str) and bool(_VALID_NAME_RE.match(name)) and name in _INTERNAL_CALLABLES


# ---------------------------------------------------------------------------
# Production allowlist — the ONLY name→callable surface (module docstring).
#
# Scheduler-unification T8/T9 append entries HERE by module edit, e.g.:
#
#     from src.memory.lifecycle import consolidate as _consolidate
#     _INTERNAL_CALLABLES["memory.consolidate"] = _consolidate
#
# Rules: literal keys only; values are imported callable objects, never
# import-path strings; nothing populates this dict from runtime input.
# Seeded EMPTY by T5 — until T6+ registers system jobs, every internal
# firing fail-closes into the executor's dispatch-failure path
# (``failure_kind="dispatch"``), which is the intended behavior.
# ---------------------------------------------------------------------------
_INTERNAL_CALLABLES: dict[str, InternalRunnable] = {}
