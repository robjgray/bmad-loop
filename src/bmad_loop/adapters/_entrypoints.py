"""Shared entry-point scanning utility for out-of-tree adapter registries.

Both the terminal-multiplexer backend registry (:mod:`multiplexer`) and the
CLI adapter dispatch in :mod:`cli` use this: a co-installed package advertises
a module under an entry-point group, and importing that module runs a
registration call as a side effect. This utility scans a group once per
process, loads each entry point, and records per-EP failures — a broken
third-party package never breaks bmad-loop.

The two registries have different domain shapes (mux backends have platform
match predicates; CLI adapters have base/dev factory pairs), so this module
only owns the entry-point loading mechanics, not the registration data
structures. Each registry keeps its own dict/list and its own
``register_*`` function.
"""

from __future__ import annotations

import importlib.metadata
import logging
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)


@dataclass
class _GroupState:
    """Per-group loader state — one instance per entry-point group."""
    loaded: bool = False
    errors: dict[str, str] = field(default_factory=dict)


# group name -> state
_STATES: dict[str, _GroupState] = {}


def _state(group: str) -> _GroupState:
    if group not in _STATES:
        _STATES[group] = _GroupState()
    return _STATES[group]


def scan_entry_points(group: str, *, entry_points_fn=None) -> dict[str, str]:
    """Scan an entry-point group, load each EP, return ``{ep_name: error}``.

    Idempotent: the first call for a given ``group`` loads every entry point
    (each EP's ``.load()`` runs the package's registration side effect);
    subsequent calls are a no-op.  Import failures are recorded per-EP and
    returned as ``{ep_name: "ExceptionType: message"}``; an empty dict means
    everything loaded (or nothing was registered).

    ``entry_points_fn`` defaults to :func:`importlib.metadata.entry_points`
    — tests monkeypatch the caller's ``importlib.metadata`` binding, so
    callers that need test-patchable behavior pass their own binding.
    """
    st = _state(group)
    if st.loaded:
        return dict(st.errors)
    st.loaded = True
    eps_fn = entry_points_fn or importlib.metadata.entry_points
    try:
        eps = eps_fn(group=group)
    except Exception as exc:  # noqa: BLE001 — diagnostics path, never crash
        st.errors["<entry-point scan>"] = f"{type(exc).__name__}: {exc}"
        return dict(st.errors)
    for ep in eps:
        try:
            ep.load()  # module import runs register_*(...)
        except Exception as exc:  # noqa: BLE001 — one bad package must not hide the rest
            st.errors[ep.name] = f"{type(exc).__name__}: {exc}"
    return dict(st.errors)


def entry_point_errors(group: str) -> dict[str, str]:
    """Return per-EP load errors for ``group`` (empty when all loaded).

    Triggers a scan if one hasn't happened yet.
    """
    st = _state(group)
    if not st.loaded:
        scan_entry_points(group)
    return dict(st.errors)


def reset_group(group: str) -> None:
    """Reset a group's loader state — for tests only."""
    st = _state(group)
    st.loaded = False
    st.errors.clear()


def reset_all() -> None:
    """Reset all groups' loader state — for tests only."""
    for st in _STATES.values():
        st.loaded = False
        st.errors.clear()