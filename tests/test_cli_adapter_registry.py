"""Tests for the out-of-tree CLI adapter entry-point registry."""

from __future__ import annotations

import pytest

from bmad_loop.adapters.registry import (
    AdapterFactory,
    register_cli_adapter,
    get_cli_adapter,
    registered_profiles,
    external_adapter_errors,
    _REGISTRY,
    _PROFILES,
    _LOADED,
    _ERRORS,
)
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
    """Reset the registry before and after each test."""
    _REGISTRY.clear()
    _PROFILES.clear()
    _ERRORS.clear()
    global _LOADED
    _LOADED = True  # prevent entry-point scan from interfering
    yield
    _REGISTRY.clear()
    _PROFILES.clear()
    _ERRORS.clear()
    _LOADED = False


def test_register_and_lookup():
    """register_cli_adapter stores the factory; get_cli_adapter retrieves it."""
    register_cli_adapter(
        "test-cli",
        base_factory=_MockAdapter,
        dev_factory=_MockDevAdapter,
    )
    factory = get_cli_adapter("test-cli")
    assert factory is not None
    assert factory.base is _MockAdapter
    assert factory.dev is _MockDevAdapter


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
    profiles = registered_profiles()
    assert "test-cli" in profiles
    assert profiles["test-cli"] == ("test_pkg.profiles", "test-cli.toml")


def test_registered_profiles_empty_by_default():
    """registered_profiles returns empty when nothing is registered."""
    assert registered_profiles() == {}


def test_external_adapter_errors_empty_by_default():
    """No errors when no entry-point scan ran (loaded flag set)."""
    assert external_adapter_errors() == {}


def test_factory_is_frozen_dataclass():
    """AdapterFactory is a frozen dataclass with base and dev fields."""
    f = AdapterFactory(base=_MockAdapter, dev=_MockDevAdapter)
    assert f.base is _MockAdapter
    assert f.dev is _MockDevAdapter
    with pytest.raises(AttributeError):
        f.base = None  # frozen