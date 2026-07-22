# CLI adapters

The orchestrator drives every agent session through a pluggable CLI-adapter seam
(`CodingCLIAdapter`). Bundled profiles (`opencode`, `claude`, `codex`, `gemini`,
`copilot`, ...) ship in the box; additional profiles install as separate packages
and register themselves automatically. This page is for adapter authors: how the
seam works, what an out-of-tree package has to do to plug in, and the reference
external adapter.

## What the seam does

A CLI adapter is a class implementing
[`CodingCLIAdapter`](src/bmad_loop/adapters/base.py) — the methods the engine
calls to start, observe, and tear down a session. Bundled adapters live in
`src/bmad_loop/adapters/`. The registration mechanism is the same shared
entry-point scan that the multiplexer backend registry uses
(`docs/multiplexer-backends.md`): an out-of-tree adapter package advertises a
module under `bmad_loop.cli_adapters`, and importing that module runs
`register_cli_adapter(profile_name=..., base_factory=..., dev_factory=...)`
which makes the adapter selectable by profile name.

The dispatch site is
[`_make_adapters`](src/bmad_loop/cli.py): for every hookless profile (one
whose adapter observes completion itself instead of via hook scripts), it
looks up the registered factory pair by `profile.name` and uses it. If no
factory is registered, it falls back to the in-tree `opencode-http` adapter — the only
bundled hookless adapter today. Hooked profiles keep using the
session-multiplexer path unchanged.

## What an out-of-tree adapter needs

Three pieces, all in one Python package:

1. **The adapter class.** One `CodingCLIAdapter` subclass (triage) and one
   for the dev/review roles that take a `paths` kwarg for spec synthesis.
   Both accept `run_dir`, `policy`, `profile`, `extra_args`,
   `usage_grace_s`, `stop_without_result_nudges` — the kwargs the engine
   passes to `_make_adapters`.
2. **A profile TOML** declaring the profile (`dialect = "none"`, no
   `transport` field, etc.). See the bundled
   `src/bmad_loop/data/profiles/opencode.toml` for the shape.
3. **An entry-point declaration** in `pyproject.toml`:

   ```toml
   [project.entry-points."bmad_loop.cli_adapters"]
   your-adapter = "your_package"
   ```

   Importing the module must call
   `register_cli_adapter(profile_name=..., base_factory=..., dev_factory=...,
   profile_package=..., profile_filename=...)` — typically as a
   module-level side effect. The registration function lives in
   `bmad_loop.adapters.profile` (alongside the profile TOML loader), and
   the entry-point scan is shared with `bmad_loop.mux_backends` via
   `bmad_loop.adapters._entrypoints`.

A complete reference is
**[bmad-loop-adapter-goose](https://github.com/robjgray/bmad-loop-adapter-goose)**,
which drives `goose acp` over stdio JSON-RPC.

## Install

The adapter package and bmad-loop are co-installed. With bmad-loop as a `uv`
tool:

```bash
uv tool install "bmad-loop @ git+https://github.com/bmad-code-org/bmad-loop.git" \
  --with "your-adapter @ git+https://github.com/you/your-adapter.git"
```

`bmad-loop validate` reports the profile; `bmad-loop init --cli your-profile`
sets it up in a project.

Two operational notes that apply to any external adapter:

- **A broken adapter package never breaks bmad-loop.** If an installed
  adapter fails to import, dispatch falls back to the in-tree
  `opencode-http` adapter and the failure is recorded in
  `bmad-loop validate`; the fix is reinstalling or upgrading the adapter.
- **No new core seam is required for a new CLI.** A new adapter is
  installable against any bmad-loop release that ships the entry-point scan
  — it does not need a matching engine change, and the engine does not need
  a matching code change to dispatch to it.