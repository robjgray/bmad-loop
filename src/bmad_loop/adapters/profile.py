"""Declarative CLI profiles for the generic tmux adapter.

A profile captures everything that differs between coding CLIs that share the
tmux-injection + hook-signal transport: binary name, how the canonical
"/skill args" prompt is rendered, bypass flags, hook registration (a config
dialect + an event-name map), and which usage parser reads the transcript.

Built-in profiles ship as packaged TOML (bmad_loop/data/profiles/*.toml) and
project-local TOML files in <project>/.bmad-loop/profiles/*.toml overlay them
(same name overrides, new names extend) — adding a CLI that clones an
existing hook dialect needs no Python.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from ..platform_util import has_parent_ref, is_absolute_path

USAGE_PARSERS = {"claude-jsonl", "codex-rollout", "gemini-chat", "copilot-events", "none"}
HOOK_DIALECTS = {
    "claude-settings-json",
    "codex-hooks-json",
    "gemini-settings-json",
    "copilot-settings-json",
    "antigravity-hooks-json",
    # hookless: the adapter observes completion itself (HTTP/SSE transport) —
    # no hook config is ever written, so config_path/events must stay empty.
    "none",
}
CANONICAL_EVENTS = {"SessionStart", "Stop", "SessionEnd", "PreCompact"}
USER_PROFILES_REL = Path(".bmad-loop") / "profiles"

# legacy adapter names from older policy.toml files, plus friendly short names
ALIASES = {"claude-code-tmux": "claude", "opencode": "opencode-http"}


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class HookSpec:
    dialect: str
    config_path: str  # project-relative, e.g. ".claude/settings.json"
    events: dict[str, str]  # native event name -> canonical event name


@dataclass(frozen=True)
class CLIProfile:
    name: str
    binary: str
    hooks: HookSpec
    # project-relative tree this CLI reads skills from, e.g. ".claude/skills"
    # (claude) or ".agents/skills" (codex/gemini); `bmad-loop init` installs the
    # bundled bmad-loop-* skills here.
    skill_tree: str = ".claude/skills"
    prompt_template: str = "{prompt}"
    launch_args: tuple[str, ...] = ()
    bypass_args: tuple[str, ...] = ()
    model_flag: str = "--model"
    env: dict[str, str] = field(default_factory=dict)
    usage_parser: str = "none"
    # seconds to keep polling the transcript for token usage after the session
    # ends. 0 = read once (the totals are already there). CLIs that flush their
    # token totals only on shutdown (Copilot writes modelMetrics in the trailing
    # session.shutdown line, ~1s after the turn-end hook) need a small grace so
    # read_usage doesn't sample the transcript before the totals land.
    usage_grace_s: float = 0.0
    # per-adapter floor for Stop-without-result nudges; None = use the global
    # limits.stop_without_result_nudges. CLIs that fire a turn-end hook PER
    # response turn (Copilot's agentStop) end a parallel-subagent phase across
    # several turns, so the global default of 1 declares them stalled too early.
    stop_without_result_nudges: int | None = None
    # Some CLIs (Copilot) fire the turn-end hook for EVERY subagent turn too, with
    # an empty transcriptPath and a tool-use session id (toolu_…) — not the main
    # session's turn-end. When true, a Stop carrying no transcript_path is treated
    # as a subagent stop and ignored, so the main session's real turn-end drives
    # completion (and supplies the transcript for usage tallying). Without this a
    # subagent's premature Stop reads as a result-less completion -> false stall.
    subagent_stop_without_transcript: bool = False
    first_run_note: str = ""
    # project-relative gitignored configs (MCP/CLI settings) this CLI needs but
    # that a `git worktree add` checkout omits; provision_worktree copies them in
    # from the main repo so isolated dev/review sessions can reach the MCP server.
    seed_files: tuple[str, ...] = ()

    @property
    def hookless(self) -> bool:
        """True for profiles whose adapter observes completion itself (HTTP/SSE)
        instead of via hook scripts — no hook config exists to register, merge,
        validate, or git-exclude."""
        return self.hooks.dialect == "none"

    def render_prompt(self, prompt: str) -> str:
        """Render the engine's canonical "/skill args" prompt for this CLI.

        Placeholders: {prompt} = the canonical string, {skill} = the leading
        slash-command name without "/", {args} = everything after it.
        """
        skill, args = "", prompt
        if prompt.startswith("/"):
            head, _, rest = prompt[1:].partition(" ")
            skill, args = head, rest.strip()
        return self.prompt_template.format(prompt=prompt, skill=skill, args=args)


def _parse_profile(doc: dict, source: str) -> CLIProfile:
    def fail(msg: str) -> ProfileError:
        return ProfileError(f"profile {source}: {msg}")

    name = str(doc.get("name", "")).strip()
    binary = str(doc.get("binary", "")).strip()
    if not name or not binary:
        raise fail("'name' and 'binary' are required")

    hooks_d = doc.get("hooks")
    if not isinstance(hooks_d, dict):
        raise fail("missing [hooks] table")
    dialect = str(hooks_d.get("dialect", ""))
    if dialect not in HOOK_DIALECTS:
        raise fail(f"hooks.dialect must be one of {sorted(HOOK_DIALECTS)}: got {dialect!r}")
    if dialect == "none":
        # hookless: nothing is ever registered, so a config_path or events map
        # is a contradiction — reject rather than silently ignore.
        if hooks_d.get("config_path") or hooks_d.get("events"):
            raise fail('hookless profiles (dialect = "none") must not set hooks.config_path/events')
        config_path = ""
        events: dict[str, str] = {}
    else:
        config_path = str(hooks_d.get("config_path", ""))
        if not config_path or is_absolute_path(config_path) or has_parent_ref(config_path):
            raise fail("hooks.config_path must be a project-relative path")
        events_d = hooks_d.get("events")
        if not isinstance(events_d, dict) or not events_d:
            raise fail("hooks.events must map native event names to canonical ones")
        events = {str(k): str(v) for k, v in events_d.items()}
        bad = sorted(set(events.values()) - CANONICAL_EVENTS)
        if bad:
            raise fail(
                f"hooks.events values must be canonical {sorted(CANONICAL_EVENTS)}: got {bad}"
            )

    usage_parser = str(doc.get("usage_parser", "none"))
    if usage_parser not in USAGE_PARSERS:
        raise fail(f"usage_parser must be one of {sorted(USAGE_PARSERS)}: got {usage_parser!r}")

    usage_grace_s = float(doc.get("usage_grace_s", 0.0))
    if usage_grace_s < 0:
        raise fail(f"usage_grace_s must be >= 0: got {usage_grace_s}")

    raw_nudges = doc.get("stop_without_result_nudges")
    stop_nudges = None if raw_nudges is None else int(raw_nudges)
    if stop_nudges is not None and stop_nudges < 0:
        raise fail(f"stop_without_result_nudges must be >= 0: got {stop_nudges}")

    skill_tree = str(doc.get("skill_tree", ".claude/skills"))
    if not skill_tree or is_absolute_path(skill_tree) or has_parent_ref(skill_tree):
        raise fail("skill_tree must be a project-relative path")

    seed_files = tuple(str(s) for s in doc.get("seed_files", ()))
    for seed in seed_files:
        if not seed or is_absolute_path(seed) or has_parent_ref(seed):
            raise fail(f"seed_files entries must be project-relative paths: got {seed!r}")

    return CLIProfile(
        name=name,
        binary=binary,
        hooks=HookSpec(dialect=dialect, config_path=config_path, events=events),
        skill_tree=skill_tree,
        prompt_template=str(doc.get("prompt_template", "{prompt}")),
        launch_args=tuple(str(a) for a in doc.get("launch_args", ())),
        bypass_args=tuple(str(a) for a in doc.get("bypass_args", ())),
        model_flag=str(doc.get("model_flag", "--model")),
        env={str(k): str(v) for k, v in doc.get("env", {}).items()},
        usage_parser=usage_parser,
        usage_grace_s=usage_grace_s,
        stop_without_result_nudges=stop_nudges,
        subagent_stop_without_transcript=bool(doc.get("subagent_stop_without_transcript", False)),
        first_run_note=str(doc.get("first_run_note", "")),
        seed_files=seed_files,
    )


def _load_toml(text: str, source: str) -> CLIProfile:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"profile {source}: invalid TOML: {e}") from e
    return _parse_profile(doc, source)


def load_profiles(project: Path | None = None) -> dict[str, CLIProfile]:
    """Packaged built-ins, overlaid by entry-point-registered profiles from
    co-installed adapter packages, overlaid by
    <project>/.bmad-loop/profiles/*.toml. Precedence (highest first):
    user-profiles, entry-point profiles, built-ins — so a project-local
    override always wins, and an adapter package's bundled profile fills a
    gap in the built-ins without shadowing a user's customization."""
    profiles: dict[str, CLIProfile] = {}
    packaged = resources.files("bmad_loop.data").joinpath("profiles")
    for entry in sorted(packaged.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".toml"):
            profile = _load_toml(entry.read_text(encoding="utf-8"), entry.name)
            profiles[profile.name] = profile
    # Entry-point-registered profiles from co-installed adapter packages
    # (bmad_loop.cli_adapters entry-point group). The entry-point scan loads
    # each adapter's __init__ module, which calls register_cli_adapter() as a
    # side effect, populating _ADAPTER_PROFILES. A broken adapter package
    # must not break profile loading — skip on any error.
    _scan_cli_adapters()  # idempotent — populates _ADAPTER_PROFILES
    for name, (pkg, filename) in _ADAPTER_PROFILES.items():
        if name not in profiles:  # built-ins win on name collision
            try:
                entry = resources.files(pkg).joinpath(filename)
                profiles[name] = _load_toml(
                    entry.read_text(encoding="utf-8"), f"{pkg}/{filename}"
                )
            except Exception:  # noqa: BLE001
                pass  # broken adapter profile must not break bmad-loop
    if project is not None:
        user_dir = project / USER_PROFILES_REL
        if user_dir.is_dir():
            for path in sorted(user_dir.glob("*.toml")):
                profile = _load_toml(path.read_text(encoding="utf-8"), str(path))
                profiles[profile.name] = profile
    return profiles


def get_profile(name: str, project: Path | None = None) -> CLIProfile:
    profiles = load_profiles(project)
    profile = profiles.get(ALIASES.get(name, name))
    if profile is None:
        raise ProfileError(f"unknown CLI profile: {name!r} (available: {sorted(profiles)})")
    return profile


# ---------------------------------------------------------------------------
# Out-of-tree CLI adapter registration
#
# Co-installed adapter packages register their adapter classes and profile
# TOML metadata here via register_cli_adapter(), called from their __init__
# module at import time. The entry-point scan (bmad_loop.cli_adapters group)
# triggers those imports. This is the same pattern as mux_backends — the
# shared _entrypoints utility owns the scan mechanics; this module owns the
# registration data.
# ---------------------------------------------------------------------------

# The entry-point group for out-of-tree CLI adapters.
CLI_ADAPTERS_GROUP = "bmad_loop.cli_adapters"

# profile_name -> (base_factory, dev_factory) — the two adapter classes
# (base = triage, dev = dev/review with spec synthesis).
_CLI_ADAPTERS: dict[str, tuple[type, type]] = {}

# profile_name -> (package, filename) — for entry-point-registered profile
# TOMLs discovered by load_profiles.
_ADAPTER_PROFILES: dict[str, tuple[str, str]] = {}


def register_cli_adapter(
    profile_name: str,
    *,
    base_factory: type,
    dev_factory: type,
    profile_package: str | None = None,
    profile_filename: str | None = None,
) -> None:
    """Register adapter classes for a hookless profile.

    Out-of-tree adapter packages call this at import time (triggered by the
    ``bmad_loop.cli_adapters`` entry-point scan). ``base_factory`` is the
    plain adapter (triage role); ``dev_factory`` is the dev/review adapter
    (takes a ``paths`` kwarg for spec synthesis). ``profile_package`` +
    ``profile_filename`` optionally register a packaged profile TOML that
    :func:`load_profiles` discovers alongside the built-in profiles.
    """
    _CLI_ADAPTERS[profile_name] = (base_factory, dev_factory)
    if profile_package and profile_filename:
        _ADAPTER_PROFILES[profile_name] = (profile_package, profile_filename)


def get_cli_adapter(profile_name: str) -> tuple[type, type] | None:
    """Return ``(base_factory, dev_factory)`` for ``profile_name``, or None."""
    _scan_cli_adapters()
    return _CLI_ADAPTERS.get(profile_name)


def _scan_cli_adapters() -> None:
    """Trigger the entry-point scan for ``bmad_loop.cli_adapters`` if it
    hasn't run yet. Each EP's module import calls register_cli_adapter() as a
    side effect, populating ``_CLI_ADAPTERS`` and ``_ADAPTER_PROFILES``.
    Idempotent — the shared utility scans once per process.
    """
    from ._entrypoints import scan_entry_points
    scan_entry_points(CLI_ADAPTERS_GROUP)


def external_adapter_errors() -> dict[str, str]:
    """Per-EP load errors for the ``bmad_loop.cli_adapters`` group.

    Empty when all adapter packages loaded (or none are installed). For
    diagnostics — surfaced by ``bmad-loop validate``.
    """
    from ._entrypoints import entry_point_errors
    return entry_point_errors(CLI_ADAPTERS_GROUP)
