"""End-to-end Windows-portable test: `bmad-loop run --story 1-1-a` driven by the
goose-acp stdio-JSON-RPC adapter against a fixture project.

Mirrors test_stories_e2e.py (the Linux-only tmux/CLAUDE E2E) but uses a
Python fake CLI that speaks the Agent Client Protocol on stdio, exercising
the GooseAcpAdapter / GooseDevAcpAdapter path end-to-end. The stdio-JSON-RPC
transport bypasses the multiplexer seam entirely (no tmux, no psmux), so the
test runs on Windows + Linux + macOS identically — the seam that proves the
goal's "MVP Goose support on Windows" claim.

What this test pins (concretely observable, not generalized):

1. The bundled goose profile (`src/bmad_loop/data/profiles/goose.toml`) is
   discoverable and resolves to a CLIProfile with `transport = "stdio-jsonrpc"`
   and `dialect = "none"`.
2. A user profile at `<project>/.bmad-loop/profiles/goose.toml` OVERRIDES the
   bundled one (the standard profile-overlay seam) and can redirect `binary`
   to a fake `goose` script.
3. `bmad-loop run --story 1-1-a` against a one-story stories.yaml drives the
   dev session through GooseDevAcpAdapter (the _DevSynthesisMixin composed
   over the ACP transport), which:
     - launches the fake `goose acp` subprocess,
     - sends initialize / session/new / session/prompt over stdio JSON-RPC,
     - parses the prompt response (`stopReason: endTurn`),
     - locates the id-keyed story spec at
       `<spec-folder>/stories/1-1-a-<slug>.md` via _DevSynthesisMixin
       (the same mixin GenericDevAdapter / OpencodeDevAdapter use),
     - synthesizes a result from `frontmatter.status: done`.
4. The run commits the story change to the main branch (one commit above the
   sandbox baseline) and exits 0.
5. The final status of story `1-1-a` is `done` (read from the id-keyed spec
   path, not the mtime scan — proving the deterministic stories-mode seam).

No LLM, no real Goose binary, no tmux: deterministic, fast, host-agnostic.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml
from conftest import install_bmad_config, install_dev_base_skills, write_script_launcher

# Spec folder + story id the goal asks us to prove. The id is "1-1-a" (Epic 1,
# Story 1, sub-letter "a") — a valid stories-mode id (ID_RE = letters/digits/
# dashes, prefix-free, no depends_on). 1-1-a resolves to a slug-keyed spec at
# <spec-folder>/stories/1-1-a-<slug>.md.
SPEC_FOLDER = "_bmad-output/epic-1"
STORY_ID = "1-1-a"
STORY_SLUG = "first-thing"  # derives the spec filename 1-1-a-first-thing.md

# Tight per-session budget so the test never waits the default 90 minutes on a
# misbehaving fake. The orchestrator reads this env var at process start, so
# setting it on the subprocess env is sufficient.
SESSION_TIMEOUT_S_ENV = "BMAD_LOOP_SESSION_TIMEOUT_S"
SESSION_TIMEOUT_S = "30"  # seconds

# A fake "goose" CLI speaking the Agent Client Protocol on stdio.
#
# Speaks just enough of the protocol to drive one turn of a stories-mode dev
# session end-to-end:
#
#   - initialize     -> protocolVersion
#   - session/new    -> returns a sessionId
#   - session/prompt -> writes the id-keyed story spec with terminal
#                       frontmatter `status: done`, makes a code change, then
#                       returns a session/prompt response with stopReason:
#                       endTurn and a usage block.
#
# Routes on the live env (`BMAD_LOOP_STORY_KEY`, `BMAD_LOOP_SPEC_FOLDER`) the
# way the real bmad-dev-auto dispatch does. Stays alive after the prompt
# response so the engine's teardown is the one that closes the subprocess
# (mirroring test_stories_e2e.py's `sleep 30`). Pure Python (no bash, no
# GNU coreutils, no setsid) so the test is portable across the same host
# matrix the stdio-jsonrpc transport itself runs on.
FAKE_GOOSE = r'''
import json
import os
import subprocess
import sys
import time

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

spec_written = set()

def write_story_spec(story_id, spec_folder, baseline):
    """Write a terminal story spec at the id-keyed path with `status: done` +
    a real code change so verify sees a worktree change. Mirrors the bash
    FAKE_CLI in test_stories_e2e.py.

    The baseline MUST be the orchestrator-recorded HEAD (the one the engine
    stamped into the task), not a placeholder — dev-decision compares the
    spec's `baseline_commit` to the task's, and a mismatch triggers
    manual-rollback (the #156 contract). bmad-dev-auto step-03 captures
    `baseline_revision` from `git rev-parse HEAD`; the fake does the same.
    """
    stories_dir = os.path.join(spec_folder, "stories")
    os.makedirs(stories_dir, exist_ok=True)
    spec_path = os.path.join(stories_dir, story_id + "-first-thing.md")
    if story_id in spec_written:
        return spec_path
    if not baseline:
        try:
            baseline = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip()
        except Exception:
            baseline = "NO_VCS"
    with open(spec_path, "w", encoding="utf-8") as fh:
        fh.write(
            "---\n"
            "title: 'Story " + story_id + "'\n"
            "status: done\n"
            "baseline_commit: '" + baseline + "'\n"
            "---\n\n"
            "# " + story_id + "\n\nimplemented by fake goose.\n"
        )
    with open("src.txt", "a", encoding="utf-8") as fh:
        fh.write("impl for " + story_id + "\n")
    spec_written.add(story_id)
    return spec_path

baseline = os.environ.get("BMAD_LOOP_BASELINE_COMMIT", "") or ""
story_id = os.environ.get("BMAD_LOOP_STORY_KEY", "")
spec_folder = os.environ.get("BMAD_LOOP_SPEC_FOLDER", "")

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    req = json.loads(line)
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "v1"}})
    elif method == "session/new":
        send({
            "jsonrpc": "2.0", "id": req_id,
            "result": {"sessionId": "session-fake-1"},
        })
    elif method == "session/prompt":
        sid = req["params"]["sessionId"]
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "Implemented " + story_id + "."},
                },
            },
        })
        if story_id and spec_folder:
            write_story_spec(story_id, spec_folder, baseline)
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "stopReason": "endTurn",
                "usage": {"totalTokens": 1, "inputTokens": 1, "outputTokens": 0},
            },
        })
    elif method == "session/close":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        # Stay alive briefly so the engine's tear-down (kill -> terminate)
        # is the one that reaps us, not a natural stdin EOF.
        time.sleep(30)
        break
'''


# A user-profile that OVERRIDES the bundled goose profile and points the
# binary at the fake launcher. Same shape as test_stories_e2e.py's
# fakestories profile, but it inherits the goose transport (stdio-jsonrpc)
# and skill tree (.agents/skills) the bundled profile declares — proving the
# bundled seam is the production surface, and the user-profile only swaps
# the binary.
#
# The binary path is ABSOLUTE (filled by `_scaffold`). On Windows,
# `subprocess.Popen(["goose"])` searches PATH but prefers `.exe` over
# `.cmd` even when the .cmd is first — a known CreateProcess quirk. Using
# the absolute path bypasses PATH search entirely. test_stories_e2e.py's
# fakestories profile uses the same absolute-path trick; this is the
# established idiom for fake-CLI tests on Windows.
def _goose_user_profile(binary_path: str) -> str:
    return f'''
name = "goose"
binary = "{binary_path}"
prompt_template = "{{prompt}}"
launch_args = ["acp"]
bypass_args = []
model_flag = "--model"
usage_parser = "none"
transport = "stdio-jsonrpc"
first_run_note = "fake goose for E2E test"
skill_tree = ".agents/skills"
seed_files = []

[env]
GOOSE_MODE = "auto"

[hooks]
dialect = "none"
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _scaffold(root: Path) -> None:
    """A committed, clean sandbox ready to run: git repo, BMAD config, base
    skill stubs (incl. the folder+id dispatch probe so stories-mode preflight
    passes), SPEC.md, a one-story stories.yaml, the fake `goose` launcher,
    the goose user-profile override, and a stories-mode policy."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "src.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text(".bmad-loop/runs/\n", encoding="utf-8")

    install_bmad_config(_paths(root))
    for sub in ("implementation-artifacts", "planning-artifacts"):
        (root / "_bmad-output" / sub).mkdir(parents=True, exist_ok=True)
        (root / "_bmad-output" / sub / ".keep").write_text("", encoding="utf-8")

    # The bundled goose profile declares skill_tree = ".agents/skills"; the
    # folder+id-capable bmad-dev-auto skill stub (with the dispatch probe the
    # stories preflight probes for) goes there.
    install_dev_base_skills(root, tree=".agents/skills", folder_id=True)

    folder = root / SPEC_FOLDER
    (folder / "stories").mkdir(parents=True)
    (folder / "SPEC.md").write_text("---\ntitle: Epic 1\n---\n# Epic 1\n", encoding="utf-8")
    entries = [{"id": STORY_ID, "title": f"Story {STORY_ID}", "description": "first thing"}]
    (folder / "stories.yaml").write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")

    # Drop a fake `goose` launcher in <project>/.bmad-loop/bin. write_script_launcher
    # writes the FAKE_GOOSE body to <name>.py and a host-appropriate launcher
    # (goose.cmd on Windows, `goose` shim on POSIX) that invokes the sidecar
    # via sys.executable.
    bin_dir = root / ".bmad-loop" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    write_script_launcher(bin_dir, "goose", FAKE_GOOSE)
    launcher = bin_dir / ("goose.cmd" if sys.platform == "win32" else "goose")

    # User-profile override: same `name = "goose"` so it overlays the bundled
    # profile. The path is normalized to forward slashes so the TOML string
    # literal doesn't see backslashes as escape sequences (\U is invalid in
    # a TOML basic string). Windows accepts forward slashes in paths.
    profiles = root / ".bmad-loop" / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    launcher_path = str(launcher).replace("\\", "/")
    (profiles / "goose.toml").write_text(_goose_user_profile(launcher_path), encoding="utf-8")

    # Stories-mode policy: stories source + spec folder + goose adapter.
    (root / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "goose"\n\n'
        "[review]\nenabled = false\n\n"
        f'[stories]\nsource = "stories"\nspec_folder = "{SPEC_FOLDER}"\n',
        encoding="utf-8",
    )

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "e2e@goose-acp")
    _git(root, "config", "user.name", "e2e goose")
    _git(root, "config", "core.fsync", "none")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sandbox")


def _paths(root: Path):
    """ProjectPaths double for conftest's install_bmad_config — only the
    `project` attribute is read there."""
    from bmad_loop.bmadconfig import ProjectPaths

    return ProjectPaths(
        project=root,
        implementation_artifacts=root / "_bmad-output" / "implementation-artifacts",
        planning_artifacts=root / "_bmad-output" / "planning-artifacts",
    )


def _commit_count(root: Path) -> int:
    out = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _run(root: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    """Invoke the real bmad-loop CLI as a subprocess, with the tight
    per-session timeout env var set."""
    env = os.environ.copy()
    env[SESSION_TIMEOUT_S_ENV] = SESSION_TIMEOUT_S
    return subprocess.run(
        [sys.executable, "-m", "bmad_loop.cli", args[0], "--project", str(root), *args[1:]],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(root),
        env=env,
    )


def _story_status(root: Path) -> str:
    spec = root / SPEC_FOLDER / "stories" / f"{STORY_ID}-{STORY_SLUG}.md"
    if not spec.is_file():
        return "pending"
    for line in spec.read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return "?"


def test_e2e_goose_acp_story_happy_path(tmp_path: Path) -> None:
    """`bmad-loop run --story 1-1-a` against a one-story project, driven by the
    GooseAcpAdapter / GooseDevAcpAdapter path (no mux, no hook scripts). The
    story ends `done`, the dev session commits, and the CLI exits 0 — proving
    the stdio-JSON-RPC transport carries the dev skill to a real terminal
    end-to-end on this host."""
    root = tmp_path / "sbx-goose"
    _scaffold(root)
    base = _commit_count(root)

    proc = _run(root, "run", "--story", STORY_ID)
    assert proc.returncode == 0, (
        f"bmad-loop run failed (rc={proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
    )
    assert _story_status(root) == "done", (
        f"story {STORY_ID} did not reach done\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
    )
    # One squashed story commit above the sandbox baseline (the dev session
    # did real work — verify's worktree-changed check would have rejected
    # an empty diff).
    assert _commit_count(root) == base + 1, (
        f"expected 1 new commit, got {_commit_count(root) - base}"
    )
