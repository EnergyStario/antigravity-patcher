"""
tests/test_conversation_rebuilder.py -- Unit tests for Antigravity Conversation Rebuilder.
Tests protobuf encoding/decoding, title resolution, prompt unwrap, and index persistence.
"""
from __future__ import annotations

import base64
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "source")))

from patcher.conversations.rebuilder import (
    encode_varint,
    decode_varint,
    skip_protobuf_field,
    strip_field_from_protobuf,
    iter_length_delimited_fields,
    encode_length_delimited,
    encode_string_field,
    build_workspace_field,
    build_timestamp_field,
    build_timestamp_fields,
    extract_timestamp_seconds,
    has_timestamp_fields,
    extract_workspace_hint,
    path_to_workspace_uri,
    uri_to_local_path,
    normalize_prompt_text,
    collapse_prompt_urls,
    truncate_prompt_title,
    format_prompt_title,
    unwrap_user_request_text,
    extract_prompt_from_conversation_db,
    build_trajectory_entry,
    write_index_to_database,
)


class TestProtobufVarint(unittest.TestCase):
    """Test low-level Protobuf varint serialization and deserialization."""

    def test_varint_roundtrip(self):
        test_values = [0, 1, 127, 128, 300, 16384, 1000000, 2147483647]
        for val in test_values:
            encoded = encode_varint(val)
            decoded, next_pos = decode_varint(encoded, 0)
            self.assertEqual(val, decoded)
            self.assertEqual(len(encoded), next_pos)

    def test_length_delimited_string(self):
        text = "Hello Antigravity!"
        encoded = encode_string_field(1, text)
        fields = list(iter_length_delimited_fields(encoded, 1))
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].decode("utf-8"), text)

    def test_strip_field_from_protobuf(self):
        # Build message with field 1 (title), field 2 (data), field 9 (workspace)
        msg = (
            encode_string_field(1, "Original Title")
            + encode_string_field(2, "Some Data")
            + encode_string_field(9, "file:///d:/Project")
        )
        # Strip field 1
        stripped = strip_field_from_protobuf(msg, 1)
        f1 = list(iter_length_delimited_fields(stripped, 1))
        f2 = list(iter_length_delimited_fields(stripped, 2))
        f9 = list(iter_length_delimited_fields(stripped, 9))

        self.assertEqual(len(f1), 0, "Field 1 should be completely removed")
        self.assertEqual(len(f2), 1, "Field 2 must be preserved")
        self.assertEqual(f2[0].decode("utf-8"), "Some Data")
        self.assertEqual(len(f9), 1, "Field 9 must be preserved")
        self.assertEqual(f9[0].decode("utf-8"), "file:///d:/Project")


class TestTimestampsAndWorkspaces(unittest.TestCase):
    """Test timestamp generation and workspace URI handling."""

    def test_timestamps(self):
        epoch = 1731283200  # Example timestamp
        ts_bytes = build_timestamp_fields(epoch)
        self.assertTrue(has_timestamp_fields(ts_bytes))
        extracted = extract_timestamp_seconds(ts_bytes, 3)
        self.assertEqual(extracted, epoch)

    def test_path_to_workspace_uri_windows(self):
        local_path = r"D:\Dev\MyProject"
        uri = path_to_workspace_uri(local_path)
        self.assertEqual(uri, "file:///d:/Dev/MyProject")

    def test_path_to_workspace_uri_remote(self):
        remote = "vscode-remote://wsl+Ubuntu/home/user/project"
        self.assertEqual(path_to_workspace_uri(remote), remote)

    def test_uri_to_local_path(self):
        uri = "file:///c:/Users/Dev/Project"
        local = uri_to_local_path(uri)
        if os.name == "nt":
            self.assertEqual(local, r"C:\Users\Dev\Project")

    def test_workspace_hint_extraction(self):
        ws_field = build_workspace_field(r"C:\MyRepo")
        hint = extract_workspace_hint(ws_field)
        self.assertIsNotNone(hint)
        self.assertIn("file:///", hint)


class TestPromptAndTitleFormatting(unittest.TestCase):
    """Test prompt cleaning, <USER_REQUEST> unwrapping, and title generation."""

    def test_unwrap_user_request(self):
        raw = "<USER_REQUEST>Fix websocket disconnection in chat handler</USER_REQUEST><ADDITIONAL_METADATA>some data</ADDITIONAL_METADATA>"
        unwrapped = unwrap_user_request_text(raw)
        self.assertEqual(unwrapped, "Fix websocket disconnection in chat handler")

    def test_collapse_urls(self):
        prompt = "Look at https://github.com/google/antigravity/issues/123 to understand why it failed."
        collapsed = collapse_prompt_urls(prompt)
        self.assertIn("github.com", collapsed)
        self.assertNotIn("https://github.com/google/antigravity/issues/123", collapsed)

    def test_format_prompt_title(self):
        prompt = "Please implement a custom protobuf serializer for session summaries."
        title = format_prompt_title(prompt)
        self.assertIsNotNone(title)
        self.assertTrue(len(title) <= 65)


class TestDatabasePromptExtraction(unittest.TestCase):
    """Test read-only SQLite step table prompt extraction."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_conversation.db")

        # Create mock conversation SQLite schema
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE steps (
                idx INTEGER PRIMARY KEY,
                step_type INTEGER,
                step_payload BLOB
            )
        """)

        # Construct protobuf payload: Field 19 (title container) -> Field 2 (prompt)
        user_prompt = "<USER_REQUEST>Refactor authentication token validation</USER_REQUEST>"
        inner_container = encode_string_field(2, user_prompt)
        full_payload = encode_length_delimited(19, inner_container)

        cur.execute("INSERT INTO steps (idx, step_type, step_payload) VALUES (1, 14, ?)", (full_payload,))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_extract_prompt_from_conversation_db(self):
        prompt = extract_prompt_from_conversation_db(self.db_path)
        self.assertIsNotNone(prompt)
        self.assertIn("Refactor authentication token validation", prompt)


class TestIndexRebuilderEndToEnd(unittest.TestCase):
    """Test end-to-end trajectory entry construction and state.vscdb update."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_db = os.path.join(self.temp_dir.name, "state.vscdb")
        conn = sqlite3.connect(self.state_db)
        cur = conn.cursor()
        cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("INSERT INTO ItemTable (key, value) VALUES ('antigravityUnifiedStateSync.trajectorySummaries', 'old_base64_data')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_build_trajectory_entry(self):
        cid = "conv-uuid-1234-5678"
        title = "Optimize database index"
        entry = build_trajectory_entry(
            conversation_id=cid,
            title=title,
            workspace_path=r"D:\Projects\Demo",
            conversation_mtime=1731283200,
        )
        self.assertTrue(len(entry) > 0)
        # Wrap as top-level trajectory item
        top_item = encode_length_delimited(1, entry)
        b64_val = base64.b64encode(top_item).decode("utf-8")

        # Test writing to database
        backup = write_index_to_database(self.state_db, b64_val, "test")
        conn = sqlite3.connect(self.state_db)
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.trajectorySummaries'")
        row = cur.fetchone()
        conn.close()

        self.assertEqual(row[0], b64_val)

    def test_unquote_workspace_entry(self):
        cid = "conv-uuid-cyrillic"
        title = "Test Cyrillic Path"
        # Create inner data with percent-encoded workspace URI
        encoded_ws = "file:///d%3A/%D0%A1%D0%BA%D1%80%D0%B8%D0%BF%D1%82/Test"
        ws_field = build_workspace_field(encoded_ws)
        inner_data = encode_string_field(1, "Old Title") + ws_field

        entry = build_trajectory_entry(
            conversation_id=cid,
            title=title,
            existing_inner_data=inner_data,
        )
        self.assertTrue(len(entry) > 0)
        hint = extract_workspace_hint(entry)
        self.assertIsNotNone(hint)
        self.assertIn("Скрипт", hint)
        self.assertNotIn("%D0%A1%D0%BA", hint)

    def test_corrupt_file_graceful_handling(self):
        # Create zero byte file
        corrupt_path = os.path.join(self.temp_dir.name, "corrupt.db")
        with open(corrupt_path, "wb") as f:
            f.write(b"NOT A SQLITE FILE")
        res = extract_prompt_from_conversation_db(corrupt_path)
        self.assertIsNone(res, "Corrupt file should return None without crashing")



if __name__ == "__main__":
    unittest.main()

