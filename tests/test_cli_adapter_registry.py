"""Tests for the out-of-tree CLI adapter entry-point registry."""

from __future__ import annotations

import pytest

from bmad_loop.adapters.profile import (
    register_cli_adapter,
    get_cli_adapter,
    external_adapter_errors,
    _CLI_ADAPTERS,
    _ADAPTER_PROFILES,
    CLI_ADAPTERS_GROUP,
)
from bmad_loop.adapters._entrypoints import reset_group
from bmad_loop.adapters.base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec


class _MockAdapter(CodingCLIAdapter):
    name = "mock"
    injection = "stdio-jsonrpc"
    observation = "rpc-response"
    state = "remote"

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        return SessionHandle(task_id=spec.task_id, native_id="mock")

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        return SessionResult(status="completed")


class _MockDevAdapter(_MockAdapter):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.paths = kwargs.get("paths")


@pytest.fixture(autouse=True)
def clean_registry():
    """Reset the adapter registry and entry-point state before and after each test."""
    _CLI_ADAPTERS.clear()
    _ADAPTER_PROFILES.clear()
    reset_group(CLI_ADAPTERS_GROUP)
    yield
    _CLI_ADAPTERS.clear()
    _ADAPTER_PROFILES.clear()
    reset_group(CLI_ADAPTERS_GROUP)


def test_register_and_lookup():
    """register_cli_adapter stores the factories; get_cli_adapter retrieves them."""
    register_cli_adapter(
        "test-cli",
        base_factory=_MockAdapter,
        dev_factory=_MockDevAdapter,
    )
    adapter = get_cli_adapter("test-cli")
    assert adapter is not None
    base, dev = adapter
    assert base is _MockAdapter
    assert dev is _MockDevAdapter


def test_lookup_missing_profile_returns_none():
    """get_cli_adapter returns None for an unregistered profile name."""
    assert get_cli_adapter("nonexistent") is None


def test_register_with_profile():
    """register_cli_adapter stores profile metadata for load_profiles discovery."""
    register_cli_adapter(
        "test-cli",
        base_factory=_MockAdapter,
        dev_factory=_MockDevAdapter,
        profile_package="test_pkg.profiles",
        profile_filename="test-cli.toml",
    )
    assert "test-cli" in _ADAPTER_PROFILES
    assert _ADAPTER_PROFILES["test-cli"] == ("test_pkg.profiles", "test-cli.toml")


def test_adapter_profiles_empty_by_default():
    """No profile metadata registered when nothing has been registered."""
    assert _ADAPTER_PROFILES == {}


def test_external_adapter_errors_empty_by_default():
    """No errors when no entry-point scan ran (reset state)."""
    assert external_adapter_errors() == {}


def test_factory_pair_is_tuple():
    """get_cli_adapter returns a (base, dev) tuple, not a dataclass."""
    register_cli_adapter(
        "test-cli",
        base_factory=_MockAdapter,
        dev_factory=_MockDevAdapter,
    )
    adapter = get_cli_adapter("test-cli")
    assert isinstance(adapter, tuple)
    assert len(adapter) == 2


class _BrokenEP:
    """Duck-typed entry point that raises on .load()."""
    def __init__(self, name):
        self.name = name

    def load(self):
        raise ImportError("No module named 'ghost_dependency'")


class _GoodEP:
    """Duck-typed entry point that registers an adapter on .load()."""
    def __init__(self, name):
        self.name = name

    def load(self):
        register_cli_adapter(
            "good-adapter",
            base_factory=_MockAdapter,
            dev_factory=_MockDevAdapter,
        )
        return None


def test_broken_entry_point_recorded_not_raised(monkeypatch):
    """A broken adapter entry-point is recorded in errors, not raised.
    The scan mechanics live in _entrypoints.py (shared with mux_backends);
    this test pins that external_adapter_errors() routes them correctly
    for the cli_adapters group."""
    import bmad_loop.adapters._entrypoints as ep_mod

    def fake_entry_points(*, group):
        assert group == CLI_ADAPTERS_GROUP
        return [_BrokenEP("broken-adapter")]

    monkeypatch.setattr(ep_mod.importlib.metadata, "entry_points", fake_entry_points)
    reset_group(CLI_ADAPTERS_GROUP)

    # get_cli_adapter triggers the scan; broken EP must not raise
    assert get_cli_adapter("broken-adapter") is None
    errors = external_adapter_errors()
    assert "broken-adapter" in errors
    assert "ghost_dependency" in errors["broken-adapter"]


def test_one_broken_package_does_not_hide_the_rest(monkeypatch):
    """Per-entry isolation: a working adapter still registers alongside a broken one."""
    import bmad_loop.adapters._entrypoints as ep_mod

    def fake_entry_points(*, group):
        assert group == CLI_ADAPTERS_GROUP
        return [_BrokenEP("broken-adapter"), _GoodEP("good-adapter")]

    monkeypatch.setattr(ep_mod.importlib.metadata, "entry_points", fake_entry_points)
    reset_group(CLI_ADAPTERS_GROUP)

    assert get_cli_adapter("good-adapter") is not None
    errors = external_adapter_errors()
    assert list(errors) == ["broken-adapter"]