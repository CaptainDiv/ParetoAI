"""`entire-agent-gemini`: an Entire Agent Integration for Gemini CLI.

Entire's native Gemini CLI support was retired (see ADR-0004); this restores it
through Entire's own documented escape hatch, the Agent Integration Protocol —
an `entire-agent-<name>` executable on `$PATH`, speaking JSON over stdin/stdout,
registered with `entire agent add gemini`. Entire's retirement code
(`retiredGeminiNameClaimed` in its source) explicitly checks for and defers to
exactly this, rather than treating a "gemini" plugin as a stray binary.

Only the required commands plus the `hooks` capability are implemented — the
pair that makes "Gemini CLI produces a real, verifiable Entire Checkpoint" true.
Entire degrades gracefully without the other optional capabilities
(transcript_analyzer, token_calculator, transcript_preparer, text_generator,
subagent_aware_extractor), and their absence is declared, not hidden: `info`
lists only `{"hooks": true}`, which is what ADR-0002's "visible, not invisible"
bar asks for here.

Two stages make up the live path:

  1. A Gemini CLI hook fires (SessionStart, BeforeAgent, AfterAgent, PreCompress,
     SessionEnd — see install_hooks). Gemini runs the command `with_managed_hooks`
     wrote into .gemini/settings.json: `entire hooks gemini <verb>`. That is
     Entire's own main binary, which resolves "gemini" to this integration and
     calls `parse_hook_payload` with Gemini's raw hook JSON on stdin.
  2. Entire then drives the rest of the lifecycle itself (get-session-id,
     get-session-dir, resolve-session-file, read-session, write-session,
     read-transcript, chunk-transcript) to assemble and store a Checkpoint.
     Everything this integration owns is bookkeeping *for* that: where a
     session's record lives, and how to read Gemini's own transcript bytes.

AgentSession bookkeeping lives under `<repo>/.entire/agents/gemini/sessions/`,
separate from Gemini's own native storage under `~/.gemini`. `session_ref` is
set to Gemini's own transcript path the first time a session is written (at
SessionStart), so every later read needs no knowledge of Gemini's internal
per-project directory naming at all — only the one-time, best-effort
`get-session-dir`/`resolve-session-file` fallback (used before any AgentSession
exists) reads Gemini's `~/.gemini/projects.json` registry via
`lookup_project_identifier`.

Hooks install into the *user*-level `~/.gemini/settings.json`, not a
repo-level `.gemini/settings.json`, confirmed live against gemini-cli 0.63.0:
a repo-level `hooks` key never reaches the live hook registry at all (`Hook
registry initialized with 0 hook entries`, no warning either way — matching a
`// TODO: loading of hooks based on workspace trust` sitting right at the call
site that builds the registry's config, in gemini-cli's own bundled source).
A user-level `hooks` key does load (`1 hook entries`, a real "Expanding hook
command" / "Created execution plan" trace). The consequence: `install_hooks`
is global, not scoped to the repos Entire manages — every Gemini CLI session
on the machine fires these hooks once installed, not just Student Agent
Attempts. `entire`'s own main binary is what's actually invoked
(`entire hooks gemini <verb>`), and it is free to no-op for a session it has
no record of, so this is a real but harmless breadth increase, not a
correctness problem — recorded in ADR-0004.

No third-party imports: this runs as a standalone binary Entire execs directly,
never through the project's venv — see `bin/entire-agent-gemini(.cmd)`, the
thin `$PATH` shim `entire agent add gemini` needs to find this file at all.

Everything above "filesystem / CLI plumbing" below is pure and TDD'd
(tests/test_entire_agent_gemini.py). Everything from there down — reading and
writing real files, talking to stdin/stdout — is live, uncovered by unit
tests, and only as trustworthy as the manual, live checks recorded in
ADR-0004: this mirrors proof_of_skill.py's own split between its pure,
TDD'd core and its live, smoke-tested GitHub calls.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from enum import IntEnum
from pathlib import Path
from typing import Any

AGENT_NAME = "gemini"
PROTOCOL_VERSION = 1

# Gemini CLI's own hook event names (geminicli.com/docs/hooks/reference), mapped
# to the verb this integration's own hook command uses. The verb is internal —
# nothing outside parse_hook_payload/with_managed_hooks needs to agree on it.
_HOOK_EVENTS: dict[str, str] = {
    "SessionStart": "session-start",
    "BeforeAgent": "before-agent",
    "AfterAgent": "after-agent",
    "PreCompress": "pre-compress",
    "SessionEnd": "session-end",
}


class EventType(IntEnum):
    """Entire's canonical Event categories (Agent Integration Protocol data model)."""

    SESSION_START = 1
    TURN_START = 2
    TURN_END = 3
    COMPACTION = 4
    SESSION_END = 5
    SUBAGENT_START = 6
    SUBAGENT_END = 7


_VERB_TO_EVENT_TYPE: dict[str, EventType] = {
    "session-start": EventType.SESSION_START,
    "before-agent": EventType.TURN_START,
    "after-agent": EventType.TURN_END,
    "pre-compress": EventType.COMPACTION,
    "session-end": EventType.SESSION_END,
}


# --- pure core ---------------------------------------------------------------


def event_type_for_hook(verb: str) -> EventType | None:
    """The Entire Event type a hook verb maps to, or None for one we don't handle."""
    return _VERB_TO_EVENT_TYPE.get(verb)


def parse_hook_payload(raw: dict[str, Any], verb: str) -> dict[str, Any] | None:
    """Gemini's raw hook JSON -> Entire's Event JSON, or None if unusable.

    None for an unrecognised verb or a payload missing session_id: either means
    this firing can't be turned into a trustworthy Event, and a guessed one
    would misattribute it to the wrong session.
    """
    event_type = event_type_for_hook(verb)
    session_id = raw.get("session_id")
    if event_type is None or not session_id:
        return None

    event: dict[str, Any] = {"type": event_type.value, "session_id": session_id}
    session_ref = raw.get("transcript_path")
    if session_ref:
        event["session_ref"] = session_ref
    timestamp = raw.get("timestamp")
    if timestamp:
        event["timestamp"] = timestamp
    return event


def replay_session_jsonl(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Gemini CLI's on-disk patch log -> the single JSON document Entire's own
    Gemini transcript compactor already expects (`{"sessionId", "messages"}`).

    Captured live against gemini-cli 0.63.0: the first line is a session header
    (sessionId, projectHash, kind, ...); every line after is either a `$set`
    merge-patch of top-level fields (one of which may be the initial
    `messages` array) or a bare message object, which is a message append.
    `$set` never re-sets `messages` to anything but that one initial array in
    practice, so treating a later `$set.messages` as a merge-patch rather than
    a second append is the conservative reading — either way no message here is
    ever duplicated or dropped.
    """
    doc: dict[str, Any] = {}
    messages: list[Any] = []
    for line in lines:
        if "$set" in line:
            patch = dict(line["$set"])
            patched_messages = patch.pop("messages", None)
            doc.update(patch)
            if patched_messages is not None:
                messages = list(patched_messages)
        elif "id" in line and "type" in line:
            messages.append(line)
        else:
            doc.update(line)
    doc["messages"] = messages
    return doc


def chunk_transcript(data: bytes, *, max_size: int) -> list[str]:
    """Split raw bytes into base64 chunks of at most `max_size` decoded bytes each."""
    if not data:
        return []
    return [
        base64.b64encode(data[start : start + max_size]).decode("ascii")
        for start in range(0, len(data), max_size)
    ]


def reassemble_transcript(chunks: list[str]) -> bytes:
    """The inverse of chunk_transcript: decode and concatenate, in order."""
    return b"".join(base64.b64decode(chunk) for chunk in chunks)


def format_resume_command(session_id: str) -> str:
    return f"gemini --resume {session_id}"


def lookup_project_identifier(registry: dict[str, Any], repo_path: str) -> str | None:
    """The short identifier Gemini CLI itself assigned `repo_path`, if any.

    Reads `~/.gemini/projects.json` rather than recomputing Gemini's own
    assignment: that registry is the only authority on which identifier (and
    which collision-breaking suffix, if any) Gemini actually picked. Matching
    is case-insensitive on the resolved path, matching Gemini CLI's own
    case-insensitive normalisation on Windows and macOS.
    """
    projects = registry.get("projects", {})
    target = str(Path(repo_path).resolve()).lower()
    for key, identifier in projects.items():
        if str(Path(key).resolve()).lower() == target:
            return identifier
    return None


def build_info() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "name": AGENT_NAME,
        "type": "cli",
        "description": "Gemini CLI, via a custom integration (Entire's own Gemini support was retired; see ADR-0004).",
        "protected_dirs": [".gemini"],
        "hook_names": sorted(_HOOK_EVENTS.values()),
        "capabilities": {"hooks": True},
    }


def _managed_command(verb: str) -> str:
    return f"entire hooks {AGENT_NAME} {verb}"


def is_managed_hook_command(command: str) -> bool:
    return command.startswith(f"entire hooks {AGENT_NAME} ")


def managed_commands_present(settings: dict[str, Any]) -> set[str]:
    """Which of this integration's own hook commands `settings` already has installed."""
    return {
        entry["command"]
        for matchers in (settings.get("hooks") or {}).values()
        for matcher in matchers
        for entry in matcher.get("hooks", [])
        if is_managed_hook_command(entry.get("command", ""))
    }


def with_managed_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    """`settings` (a Gemini CLI `.gemini/settings.json`) with every managed hook
    event present, every other key and hook untouched.

    Idempotent: a second pass finds its own entries already present (by
    command string) and adds nothing more, so a plain re-enable never
    duplicates an entry.
    """
    result = dict(settings)
    hooks = dict(result.get("hooks") or {})
    for event, verb in _HOOK_EVENTS.items():
        matchers = list(hooks.get(event) or [])
        command = _managed_command(verb)
        already_present = any(
            entry.get("command") == command
            for matcher in matchers
            for entry in matcher.get("hooks", [])
        )
        if not already_present:
            matchers.append({"matcher": "*", "hooks": [{"type": "command", "command": command}]})
        hooks[event] = matchers
    result["hooks"] = hooks
    return result


def strip_managed_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    """The inverse of with_managed_hooks: every entry this integration installed
    removed, everything else (including other integrations' hooks) untouched.
    """
    result = dict(settings)
    hooks_raw = result.get("hooks")
    if not isinstance(hooks_raw, dict):
        return result

    hooks = dict(hooks_raw)
    for event in list(hooks.keys()):
        kept_matchers = []
        for matcher in hooks[event]:
            kept_entries = [
                entry for entry in matcher.get("hooks", []) if not is_managed_hook_command(entry.get("command", ""))
            ]
            if kept_entries:
                kept_matchers.append({**matcher, "hooks": kept_entries})
        if kept_matchers:
            hooks[event] = kept_matchers
        else:
            del hooks[event]

    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result


# --- filesystem / CLI plumbing ------------------------------------------------


def _agent_session_dir(repo_path: str) -> Path:
    return Path(repo_path) / ".entire" / "agents" / AGENT_NAME / "sessions"


def _read_json_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def _print_json(value: Any) -> None:
    print(json.dumps(value))


def _cmd_info(_args: list[str]) -> int:
    _print_json(build_info())
    return 0


def _cmd_detect(_args: list[str]) -> int:
    _print_json({"present": shutil.which("gemini") is not None})
    return 0


def _cmd_get_session_id(_args: list[str]) -> int:
    hook_input = _read_json_stdin()
    _print_json({"session_id": hook_input.get("session_id", "")})
    return 0


def _flag(args: list[str], name: str) -> str | None:
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
    return None


def _cmd_get_session_dir(args: list[str]) -> int:
    repo_path = _flag(args, "--repo-path") or os.getcwd()
    session_dir = _agent_session_dir(repo_path)
    session_dir.mkdir(parents=True, exist_ok=True)
    _print_json({"session_dir": str(session_dir)})
    return 0


def _cmd_resolve_session_file(args: list[str]) -> int:
    session_dir = _flag(args, "--session-dir") or str(_agent_session_dir(os.getcwd()))
    session_id = _flag(args, "--session-id") or ""
    _print_json({"session_file": str(Path(session_dir) / f"{session_id}.json")})
    return 0


def _session_file_for(repo_path: str, session_id: str) -> Path:
    return _agent_session_dir(repo_path) / f"{session_id}.json"


def _cmd_read_session(_args: list[str]) -> int:
    hook_input = _read_json_stdin()
    session_id = hook_input.get("session_id", "")
    path = _session_file_for(os.getcwd(), session_id)
    if path.exists():
        _print_json(json.loads(path.read_text(encoding="utf-8")))
    else:
        _print_json(
            {
                "session_id": session_id,
                "agent_name": AGENT_NAME,
                "repo_path": os.getcwd(),
                "session_ref": hook_input.get("session_ref", ""),
                "modified_files": [],
                "new_files": [],
                "deleted_files": [],
            }
        )
    return 0


def _cmd_write_session(_args: list[str]) -> int:
    session = _read_json_stdin()
    repo_path = session.get("repo_path") or os.getcwd()
    path = _session_file_for(repo_path, session.get("session_id", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session), encoding="utf-8")
    return 0


def _cmd_read_transcript(args: list[str]) -> int:
    session_ref = _flag(args, "--session-ref") or ""
    lines = [
        json.loads(line)
        for line in Path(session_ref).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    document = replay_session_jsonl(lines)
    sys.stdout.buffer.write(json.dumps(document).encode("utf-8"))
    return 0


def _cmd_chunk_transcript(args: list[str]) -> int:
    max_size = int(_flag(args, "--max-size") or "65536")
    data = sys.stdin.buffer.read()
    _print_json({"chunks": chunk_transcript(data, max_size=max_size)})
    return 0


def _cmd_reassemble_transcript(_args: list[str]) -> int:
    payload = _read_json_stdin()
    sys.stdout.buffer.write(reassemble_transcript(payload.get("chunks", [])))
    return 0


def _cmd_format_resume_command(args: list[str]) -> int:
    session_id = _flag(args, "--session-id") or ""
    _print_json({"command": format_resume_command(session_id)})
    return 0


def _gemini_settings_path() -> Path:
    """Where hooks are installed: the user-level settings file.

    Not `<repo>/.gemini/settings.json` — confirmed live that gemini-cli 0.63.0
    never loads a repo-level `hooks` key into its live registry (see module
    docstring). This is the one file that does take effect.
    """
    return Path.home() / ".gemini" / "settings.json"


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _cmd_parse_hook(args: list[str]) -> int:
    verb = _flag(args, "--hook") or ""
    raw = _read_json_stdin()
    event = parse_hook_payload(raw, verb)
    _print_json(event)
    return 0


def _cmd_install_hooks(_args: list[str]) -> int:
    path = _gemini_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    before = _load_settings(path)
    after = with_managed_hooks(before)
    path.write_text(json.dumps(after, indent=2) + "\n", encoding="utf-8")
    newly_installed = managed_commands_present(after) - managed_commands_present(before)
    _print_json({"hooks_installed": len(newly_installed)})
    return 0


def _cmd_uninstall_hooks(_args: list[str]) -> int:
    path = _gemini_settings_path()
    if path.exists():
        path.write_text(json.dumps(strip_managed_hooks(_load_settings(path)), indent=2) + "\n", encoding="utf-8")
    return 0


def _cmd_are_hooks_installed(_args: list[str]) -> int:
    settings = _load_settings(_gemini_settings_path())
    managed_commands = {_managed_command(verb) for verb in _HOOK_EVENTS.values()}
    _print_json({"installed": managed_commands <= managed_commands_present(settings)})
    return 0


_COMMANDS = {
    "info": _cmd_info,
    "detect": _cmd_detect,
    "get-session-id": _cmd_get_session_id,
    "get-session-dir": _cmd_get_session_dir,
    "resolve-session-file": _cmd_resolve_session_file,
    "read-session": _cmd_read_session,
    "write-session": _cmd_write_session,
    "read-transcript": _cmd_read_transcript,
    "chunk-transcript": _cmd_chunk_transcript,
    "reassemble-transcript": _cmd_reassemble_transcript,
    "format-resume-command": _cmd_format_resume_command,
    "parse-hook": _cmd_parse_hook,
    "install-hooks": _cmd_install_hooks,
    "uninstall-hooks": _cmd_uninstall_hooks,
    "are-hooks-installed": _cmd_are_hooks_installed,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in _COMMANDS:
        print(f"usage: entire-agent-gemini <{'|'.join(_COMMANDS)}> [args]", file=sys.stderr)
        return 1
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
