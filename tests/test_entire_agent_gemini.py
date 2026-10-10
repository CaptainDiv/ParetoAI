import base64
import json

from pareto_ai.entire_agent_gemini import (
    AGENT_NAME,
    EventType,
    build_info,
    chunk_transcript,
    event_type_for_hook,
    format_resume_command,
    lookup_project_identifier,
    managed_commands_present,
    parse_hook_payload,
    reassemble_transcript,
    replay_session_jsonl,
    strip_managed_hooks,
    with_managed_hooks,
)

# --- event_type_for_hook -----------------------------------------------------


def test_session_start_maps_to_session_start_event():
    assert event_type_for_hook("session-start") == EventType.SESSION_START


def test_before_agent_maps_to_turn_start():
    assert event_type_for_hook("before-agent") == EventType.TURN_START


def test_after_agent_maps_to_turn_end():
    assert event_type_for_hook("after-agent") == EventType.TURN_END


def test_pre_compress_maps_to_compaction():
    assert event_type_for_hook("pre-compress") == EventType.COMPACTION


def test_session_end_maps_to_session_end():
    assert event_type_for_hook("session-end") == EventType.SESSION_END


def test_an_unknown_hook_name_maps_to_nothing():
    assert event_type_for_hook("before-tool") is None


# --- parse_hook_payload -------------------------------------------------------


def raw_gemini_hook(**overrides) -> dict:
    payload = {
        "session_id": "5952a7f5-394d-4ece-9e2d-dc36d6e16738",
        "transcript_path": r"C:\Users\user\.gemini\tmp\gemini-probe\chats\session-1.jsonl",
        "cwd": r"C:\Users\user\pareto_ai",
        "hook_event_name": "session-start",
        "timestamp": "2026-10-10T19:47:27.280Z",
    }
    payload.update(overrides)
    return payload


def test_a_known_hook_becomes_an_event_with_its_type_and_session():
    event = parse_hook_payload(raw_gemini_hook(), "session-start")
    assert event == {
        "type": EventType.SESSION_START.value,
        "session_id": "5952a7f5-394d-4ece-9e2d-dc36d6e16738",
        "session_ref": r"C:\Users\user\.gemini\tmp\gemini-probe\chats\session-1.jsonl",
        "timestamp": "2026-10-10T19:47:27.280Z",
    }


def test_an_unrecognised_hook_name_is_refused_rather_than_guessed():
    assert parse_hook_payload(raw_gemini_hook(), "notification") is None


def test_a_payload_missing_a_session_id_is_refused():
    payload = raw_gemini_hook()
    del payload["session_id"]
    assert parse_hook_payload(payload, "session-start") is None


# --- replay_session_jsonl ------------------------------------------------------


def test_the_header_line_seeds_the_session_id():
    doc = replay_session_jsonl([{"sessionId": "abc", "kind": "main"}])
    assert doc == {"sessionId": "abc", "kind": "main", "messages": []}


def test_a_set_operation_merges_a_messages_array_in():
    lines = [
        {"sessionId": "abc"},
        {"$set": {"messages": [{"id": "1", "type": "user", "content": "hi"}]}},
    ]
    doc = replay_session_jsonl(lines)
    assert doc["messages"] == [{"id": "1", "type": "user", "content": "hi"}]


def test_a_bare_message_object_is_appended_rather_than_replacing_the_array():
    lines = [
        {"sessionId": "abc"},
        {"$set": {"messages": [{"id": "1", "type": "user", "content": "hi"}]}},
        {"id": "2", "type": "gemini", "content": "pong"},
    ]
    doc = replay_session_jsonl(lines)
    assert [m["id"] for m in doc["messages"]] == ["1", "2"]


def test_a_set_that_only_touches_lastupdated_does_not_duplicate_messages():
    lines = [
        {"sessionId": "abc"},
        {"$set": {"messages": [{"id": "1", "type": "user", "content": "hi"}]}},
        {"id": "2", "type": "gemini", "content": "pong"},
        {"$set": {"lastUpdated": "2026-10-10T19:47:39.218Z"}},
    ]
    doc = replay_session_jsonl(lines)
    assert [m["id"] for m in doc["messages"]] == ["1", "2"]
    assert doc["lastUpdated"] == "2026-10-10T19:47:39.218Z"


def test_an_empty_log_yields_no_messages_rather_than_raising():
    assert replay_session_jsonl([]) == {"messages": []}


def test_the_real_captured_session_format_replays_to_three_messages():
    # Captured live from gemini-cli 0.63.0: header, an initial $set seeding one
    # context message, a bare user message, a lastUpdated-only $set, a bare
    # gemini reply, and a final lastUpdated-only $set.
    lines = [
        json.loads(line)
        for line in [
            '{"sessionId":"5952a7f5","kind":"main"}',
            '{"$set":{"messages":[{"id":"ctx","timestamp":"t0","type":"user","content":[{"text":"<session_context>"}]}],"lastUpdated":"t0"}}',
            '{"id":"u1","timestamp":"t1","type":"user","content":[{"text":"Reply with exactly the word: pong"}]}',
            '{"$set":{"lastUpdated":"t1b"}}',
            '{"id":"g1","timestamp":"t2","type":"gemini","content":"pong","tokens":{"input":1,"output":1}}',
            '{"$set":{"lastUpdated":"t2b"}}',
        ]
    ]
    doc = replay_session_jsonl(lines)
    assert [m["id"] for m in doc["messages"]] == ["ctx", "u1", "g1"]
    assert doc["sessionId"] == "5952a7f5"
    assert doc["lastUpdated"] == "t2b"


# --- chunk_transcript / reassemble_transcript ----------------------------------


def test_data_shorter_than_max_size_is_one_chunk():
    chunks = chunk_transcript(b"hello", max_size=1024)
    assert len(chunks) == 1
    assert base64.b64decode(chunks[0]) == b"hello"


def test_data_longer_than_max_size_splits_into_multiple_chunks():
    data = b"x" * 2500
    chunks = chunk_transcript(data, max_size=1000)
    assert len(chunks) == 3
    assert all(len(base64.b64decode(c)) <= 1000 for c in chunks)


def test_chunking_then_reassembling_round_trips_exactly():
    data = bytes(range(256)) * 10
    chunks = chunk_transcript(data, max_size=37)
    assert reassemble_transcript(chunks) == data


def test_empty_data_chunks_to_nothing_and_reassembles_to_empty():
    assert chunk_transcript(b"", max_size=100) == []
    assert reassemble_transcript([]) == b""


# --- format_resume_command -----------------------------------------------------


def test_format_resume_command_names_the_session_id():
    assert format_resume_command("abc-123") == "gemini --resume abc-123"


# --- lookup_project_identifier --------------------------------------------------


def test_finds_the_identifier_gemini_cli_registered_for_this_path():
    registry = {"projects": {r"c:\users\user\appdata\local\temp\gemini-probe": "gemini-probe"}}
    identifier = lookup_project_identifier(
        registry, r"C:\Users\user\AppData\Local\Temp\gemini-probe"
    )
    assert identifier == "gemini-probe"


def test_a_path_gemini_cli_never_registered_is_not_found():
    registry = {"projects": {r"c:\some\other\repo": "other-repo"}}
    assert lookup_project_identifier(registry, r"C:\Users\user\pareto_ai") is None


def test_an_empty_registry_finds_nothing():
    assert lookup_project_identifier({}, r"C:\anything") is None


# --- build_info -----------------------------------------------------------------


def test_info_declares_the_agent_name_and_protocol_version():
    info = build_info()
    assert info["name"] == AGENT_NAME == "gemini"
    assert info["protocol_version"] == 1


def test_info_declares_hooks_capability_and_nothing_it_does_not_implement():
    info = build_info()
    assert info["capabilities"] == {"hooks": True}


def test_info_names_gemini_settings_json_as_a_protected_dir():
    assert ".gemini" in build_info()["protected_dirs"]


# --- with_managed_hooks / strip_managed_hooks (install/uninstall-hooks core) ----


def test_installing_into_an_empty_settings_file_adds_every_managed_event():
    settings = with_managed_hooks({})
    names = {hook["type"] for hook in settings["hooks"]} if isinstance(settings["hooks"], list) else None
    # hooks is an object keyed by event name per Gemini CLI's schema
    assert set(settings["hooks"].keys()) == {
        "SessionStart",
        "BeforeAgent",
        "AfterAgent",
        "PreCompress",
        "SessionEnd",
    }


def test_installing_preserves_an_unrelated_existing_hook():
    existing = {
        "hooks": {
            "BeforeTool": [{"matcher": "Shell", "hooks": [{"type": "command", "command": "my-linter"}]}]
        }
    }
    settings = with_managed_hooks(existing)
    assert settings["hooks"]["BeforeTool"] == existing["hooks"]["BeforeTool"]
    assert "SessionStart" in settings["hooks"]


def test_installing_preserves_an_unrelated_top_level_setting():
    settings = with_managed_hooks({"theme": "dark"})
    assert settings["theme"] == "dark"


def test_installing_twice_does_not_duplicate_entries():
    once = with_managed_hooks({})
    twice = with_managed_hooks(once)
    assert twice == once


def test_every_managed_hook_command_dispatches_through_entire():
    settings = with_managed_hooks({})
    for event, matchers in settings["hooks"].items():
        for matcher in matchers:
            for entry in matcher["hooks"]:
                assert entry["command"] == "entire hooks gemini " + {
                    "SessionStart": "session-start",
                    "BeforeAgent": "before-agent",
                    "AfterAgent": "after-agent",
                    "PreCompress": "pre-compress",
                    "SessionEnd": "session-end",
                }[event]


def test_uninstalling_removes_every_managed_entry():
    installed = with_managed_hooks({})
    stripped = strip_managed_hooks(installed)
    assert "hooks" not in stripped or stripped["hooks"] == {}


def test_uninstalling_leaves_an_unrelated_hook_in_place():
    existing = {
        "hooks": {
            "BeforeTool": [{"matcher": "Shell", "hooks": [{"type": "command", "command": "my-linter"}]}]
        }
    }
    installed = with_managed_hooks(existing)
    stripped = strip_managed_hooks(installed)
    assert stripped["hooks"]["BeforeTool"] == existing["hooks"]["BeforeTool"]
    assert "SessionStart" not in stripped["hooks"]


def test_uninstalling_an_untouched_settings_file_changes_nothing():
    settings = {"theme": "dark"}
    assert strip_managed_hooks(settings) == settings


# --- managed_commands_present ---------------------------------------------------


def test_an_empty_settings_file_has_no_managed_commands():
    assert managed_commands_present({}) == set()


def test_finds_every_managed_command_once_installed():
    installed = with_managed_hooks({})
    assert len(managed_commands_present(installed)) == 5


def test_does_not_count_an_unrelated_hook_as_managed():
    existing = {
        "hooks": {
            "BeforeTool": [{"matcher": "Shell", "hooks": [{"type": "command", "command": "my-linter"}]}]
        }
    }
    assert managed_commands_present(existing) == set()
