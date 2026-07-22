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