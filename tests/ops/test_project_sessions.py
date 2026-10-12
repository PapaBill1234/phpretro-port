"""Verify private history scope and complete pagination across real file contents."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sessions", REPO / "scripts/project-sessions.py")
sessions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sessions)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "config").mkdir()
        (self.root / "config/doctor-scope.json").write_text(json.dumps({"worktrees": ["luna-backend"]}))
        self.history = self.root / ".builder/claude/projects"
        self.main = self.history / sessions.encoded(self.root)
        self.main.mkdir(parents=True)
        (self.main / "main.jsonl").write_text('{"message":"PHPRetro ☃"}\n' + 'λ' * 70000, encoding="utf-8")
        (self.main / "subagents").mkdir()
        (self.main / "subagents/agent-test.jsonl").write_text('{"message":"worker evidence"}\n')
        worker = self.history / sessions.encoded(self.root / ".claude/worktrees/luna-backend")
        worker.mkdir()
        (worker / "worker.jsonl").write_text('{"message":"saved worker"}\n')
        foreign = self.history / sessions.encoded(self.root.parent / "failed-project")
        foreign.mkdir()
        self.foreign = foreign / "foreign.jsonl"
        self.foreign.write_text('{"message":"DO NOT INCLUDE foreign"}\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_only_current_and_named_worker_and_nested_subagent_sessions(self):
        entries = sessions.catalog(self.root)
        self.assertEqual(len(entries), 3)
        self.assertEqual({e["kind"] for e in entries}, {"claude", "claude-subagent"})
        self.assertEqual(sessions.search("DO NOT INCLUDE", root=self.root)["hits"], [])

    def test_future_registered_worker_is_indexed_without_global_scan(self):
        sessions.history_registry.register_worktree(self.root, "future-builder")
        path = self.history / sessions.encoded(self.root / ".claude/worktrees/future-builder")
        path.mkdir()
        (path / "future.jsonl").write_text('{"message":"future saved session"}')
        self.assertEqual(len(sessions.catalog(self.root)), 4)
        self.assertEqual(len(sessions.search("future saved session", root=self.root)["hits"]), 1)
        self.assertEqual(sessions.search("DO NOT INCLUDE", root=self.root)["hits"], [])

    def test_complete_unicode_pagination_including_long_records(self):
        entry = next(e for e in sessions.catalog(self.root) if e["path"].endswith("main.jsonl"))
        offset, pieces = 0, []
        while offset is not None:
            result = sessions.read_session(entry["id"], offset, 997, self.root)
            pieces.append(result["content"])
            offset = result["next_offset"]
        self.assertEqual("".join(pieces), (self.main / "main.jsonl").read_text())

    def test_indexed_reads_reject_leaf_and_parent_replacements(self):
        for operation in ("read", "search"):
            target = self.main / "main.jsonl"
            original = sessions.catalog

            def swap_leaf(root=self.root):
                entries = original(root)
                target.unlink()
                target.symlink_to(self.foreign)
                return entries

            with mock.patch.object(sessions, "catalog", side_effect=swap_leaf):
                with self.assertRaises((ValueError, OSError)):
                    if operation == "read":
                        sessions.read_session(next(e for e in original(self.root) if e["path"].endswith("main.jsonl"))["id"], root=self.root)
                    else:
                        sessions.search("DO NOT INCLUDE", root=self.root)
            target.unlink(missing_ok=True)
            target.write_text("restored", encoding="utf-8")

        directory = self.main
        moved = self.history / "main-moved"
        original = sessions.catalog

        def swap_parent(root=self.root):
            entries = original(root)
            directory.rename(moved)
            directory.symlink_to(self.history / sessions.encoded(self.root.parent / "failed-project"), target_is_directory=True)
            return entries

        with mock.patch.object(sessions, "catalog", side_effect=swap_parent):
            with self.assertRaises((ValueError, OSError)):
                sessions.search("DO NOT INCLUDE", root=self.root)

    def test_indexed_reads_reject_regular_inode_replacement_but_allow_append(self):
        target = self.main / "main.jsonl"
        entry = next(e for e in sessions.catalog(self.root) if e["path"].endswith("main.jsonl"))
        with target.open("a", encoding="utf-8") as stream:
            stream.write(" appended")
        self.assertEqual(len(sessions.search("appended", root=self.root)["hits"]), 1)

        original = sessions.catalog

        def replace(root=self.root):
            entries = original(root)
            replacement = target.with_name("replacement.jsonl")
            replacement.write_text("replacement", encoding="utf-8")
            target.unlink()
            replacement.rename(target)
            return entries

        with mock.patch.object(sessions, "catalog", side_effect=replace):
            with self.assertRaises(ValueError):
                sessions.search("replacement", root=self.root)

    def test_unknown_ids_traversal_symlink_and_invalid_page_rejected(self):
        (self.main / "escape.jsonl").symlink_to(self.foreign)
        self.assertEqual(len(sessions.catalog(self.root)), 3)
        for value in ("../../foreign", "unknown"):
            with self.assertRaises(ValueError):
                sessions.read_session(value, root=self.root)
        entry = sessions.catalog(self.root)[0]
        for offset, limit in ((-1, 10), (0, 0), (0, 65537)):
            with self.assertRaises(ValueError):
                sessions.read_session(entry["id"], offset, limit, self.root)

    def test_literal_search_pagination_and_current_snapshot(self):
        imported = self.root / ".builder/doctor/imports/current-codex.jsonl"
        imported.parent.mkdir(parents=True)
        imported.write_text('literal [pattern]\nliteral [pattern]\nliteral [pattern]')
        unrelated = imported.with_name("other-project.jsonl")
        unrelated.write_text('DO NOT INCLUDE')
        self.assertEqual(len(sessions.catalog(self.root)), 4)
        first = sessions.search("[pattern]", limit=2, root=self.root)
        self.assertEqual(len(first["hits"]), 2)
        second = sessions.search("[pattern]", offset=first["next_offset"], limit=2, root=self.root)
        self.assertEqual(len(second["hits"]), 1)
        self.assertIsNone(second["next_offset"])
        self.assertEqual(sessions.search("DO NOT INCLUDE", root=self.root)["hits"], [])


if __name__ == "__main__":
    unittest.main()
