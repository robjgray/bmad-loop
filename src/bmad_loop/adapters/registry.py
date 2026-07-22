"""Registry for out-of-tree CLI adapters.

Mirrors the multiplexer backend registry (:mod:`bmad_loop.adapters.multiplexer`):
an entry-point group (``bmad_loop.cli_adapters``) lets a co-installed package
register adapter factories keyed by profile name, so ``_make_adapters`` can
dispatch hookless profiles to the right adapter without hardcoding imports.
A broken adapter package never breaks bmad-loop — import failures are recorded
and surfaced by ``validate`` and ``diagnose``, not raised. See
:doc:`/docs/cli-adapters` for the adapter-author contract.
"""

from __future__ import annotations

import importlib.metadata
import logging
from dataclasses import dataclass
from typing import Callable

from .base import CodingCLIAdapter

_log = logging.getLogger(__name__)

# The entry-point group an out-of-tree CLI adapter package advertises its
# module under; importing the module runs register_cli_adapter(...) at import
# time. Mirrors bmad_loop.mux_backends.
CLI_ADAPTERS_GROUP = "bmad_loop.cli_adapters"

# profile_name -> factory pair
_REGISTRY: dict[str, AdapterFactory] = {}
# profile_name -> (package, filename) for entry-point-registered profile TOMLs
_PROFILES: dict[str, tuple[str, str]] = {}
_LOADED = False
_ERRORS: dict[str, str] = {}


@dataclass(frozen=True)
class AdapterFactory:
    """Factory pair for a hookless adapter: base + dev (synthesizing) variants.

    ``base`` is the plain adapter (triage role); ``dev`` is the dev/review
    adapter (takes a ``paths`` kwarg for spec synthesis). Both accept the
    common kwargs from ``_make_adapters``: ``run_dir``, ``policy``,
    ``profile``, ``extra_args``, ``usage_grace_s``,
    ``stop_without_result_nudges``.
    """

    base: Callable[..., CodingCLIAdapter]
    dev: Callable[..., CodingCLIAdapter]


def register_cli_adapter(
    profile_name: str,
    *,
    base_factory: Callable[..., CodingCLIAdapter],
    dev_factory: Callable[..., CodingCLIAdapter],
    profile_package: str | None = None,
    profile_filename: str | None = None,
) -> None:
    """Register adapter factories for a hookless profile.

    Out-of-tree adapter packages call this at import time (triggered by the
    ``bmad_loop.cli_adapters`` entry-point scan in
    :func:`_load_external_adapters`). ``profile_package`` +
    ``profile_filename`` optionally register a packaged profile TOML that
    :func:`bmad_loop.adapters.profile.load_profiles` discovers alongside the
    built-in profiles.
    """
    _REGISTRY[profile_name] = AdapterFactory(base=base_factory, dev=dev_factory)
    if profile_package and profile_filename:
        _PROFILES[profile_name] = (profile_package, profile_filename)


def get_cli_adapter(profile_name: str) -> AdapterFactory | None:
    """Return the factory pair for ``profile_name``, or None if unregistered."""
    _load_external_adapters()
    return _REGISTRY.get(profile_name)


def registered_profiles() -> dict[str, tuple[str, str]]:
    """Entry-point-registered profile TOMLs: name -> (package, filename)."""
    _load_external_adapters()
    return dict(_PROFILES)


def external_adapter_errors() -> dict[str, str]:
    """Entry-point name -> failure reason for every external adapter that
    failed to load this process (empty when all loaded). For diagnostics."""
    _load_external_adapters()
    return dict(_ERRORS)


def _load_external_adapters() -> None:
    """Import every ``bmad_loop.cli_adapters`` entry point; each module
    self-registers via :func:`register_cli_adapter` at import time.

    A broken third-party distribution must never break adapter selection:
    failures are recorded in ``_ERRORS`` (surfaced by ``validate`` and
    ``diagnose``), not raised. The loaded-flag is set up front: a third-party
    import failure is not transient, and retrying on every lookup would
    re-import (and re-fail) each time.
    """
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        eps = importlib.metadata.entry_points(group=CLI_ADAPTERS_GROUP)
    except Exception as exc:  # noqa: BLE001
        _ERRORS["<entry-point scan>"] = f"{type(exc).__name__}: {exc}"
        return
    for ep in eps:
        try:
            ep.load()  # module import runs register_cli_adapter(...)
        except Exception as exc:  # noqa: BLE001
            _ERRORS[ep.name] = f"{type(exc).__name__}: {exc}"