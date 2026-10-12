import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("task_board", Path(__file__).resolve().parents[2] / "scripts/task-board.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = board.ROOT
        board.ROOT = self.root
        self.db = self.root / ".builder/tasks.sqlite"
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.root / "domain").mkdir()
        (self.root / "domain/a.txt").write_text("before")
        self.git("add", "domain")
        self.git("commit", "-m", "fixture base")

    def tearDown(self):
        board.ROOT = self.previous
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def call(self, *args):
        parsed = board.parser().parse_args(["--db", str(self.db), *args])
        board.run(parsed)

    def add(self, task, scope="domain", depends=None):
        self.call("add", task, "--scope", scope, "--depends", *(depends or []))

    def test_concurrent_overlapping_claims_have_one_winner(self):
        self.add("one"); self.add("two", "domain/a.txt")
        def claim(task):
            try:
                self.call("claim", task, "--owner", task)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(claim, ["one", "two"])), 1)

    def test_dependency_and_owner_are_checked(self):
        self.add("first"); self.add("next", "other", ["first"])
        with self.assertRaises(ValueError):
            self.call("claim", "next", "--owner", "worker")
        self.call("claim", "first", "--owner", "worker")
        with self.assertRaises(ValueError):
            self.call("renew", "first", "--owner", "different")

    def test_finish_rejects_cross_scope_diff(self):
        self.add("one"); self.call("claim", "one", "--owner", "worker")
        (self.root / "unowned.txt").write_text("bad scope")
        self.git("add", "unowned.txt"); self.git("commit", "-m", "cross scope")
        (self.root / ".builder/evidence.md").write_text("fixture evidence")
        with self.assertRaises(ValueError):
            self.call("finish", "one", "--owner", "worker", "--commit", "HEAD", "--evidence", ".builder/evidence.md", "--checks", "fixture")

    def test_review_is_independent_and_required_before_integration(self):
        self.add("one"); self.call("claim", "one", "--owner", "worker")
        (self.root / "domain/a.txt").write_text("after")
        self.git("add", "domain"); self.git("commit", "-m", "owned diff")
        (self.root / ".builder/evidence.md").write_text("observable check fixture")
        self.call("finish", "one", "--owner", "worker", "--commit", "HEAD", "--evidence", ".builder/evidence.md", "--checks", "fixture")
        with self.assertRaises(ValueError):
            self.call("integrate", "one", "--commit", "HEAD")
        with self.assertRaises(ValueError):
            self.call("review", "one", "--reviewer", "worker", "--result", "accepted", "--note", "self review")
        self.call("review", "one", "--reviewer", "reviewer", "--result", "accepted", "--note", "fixture verified")
        self.call("integrate", "one", "--commit", "HEAD")
        db = board.connect(self.db)
        self.assertEqual(db.execute("SELECT status FROM tasks WHERE id='one'").fetchone()[0], "integrated")
        db.close()

    def test_release_requires_explicit_worktree_inspection(self):
        self.add("one"); self.call("claim", "one", "--owner", "worker")
        with self.assertRaises(ValueError):
            self.call("release", "one", "--owner", "worker")
        self.call("release", "one", "--owner", "worker", "--inspected")
        self.call("claim", "one", "--owner", "next-worker")

    def test_rename_cannot_delete_an_unowned_source(self):
        (self.root / "outside.txt").write_text("outside scope")
        self.git("add", "outside.txt"); self.git("commit", "-m", "unowned baseline")
        self.add("one"); self.call("claim", "one", "--owner", "worker")
        self.git("mv", "outside.txt", "domain/renamed.txt")
        self.git("commit", "-m", "rename into owned scope")
        (self.root / ".builder/evidence.md").write_text("fixture evidence")
        with self.assertRaises(ValueError):
            self.call("finish", "one", "--owner", "worker", "--commit", "HEAD", "--evidence", ".builder/evidence.md", "--checks", "fixture")

    def test_accepted_dependency_allows_descendant_overlap_and_preserves_record(self):
        self.add("prior"); self.call("claim", "prior", "--owner", "worker")
        self.git("checkout", "-b", "auto/prior")
        (self.root / "domain/a.txt").write_text("prior change")
        self.git("add", "domain"); self.git("commit", "-m", "prior commit")
        prior_commit = self.git("rev-parse", "HEAD")
        (self.root / ".builder/evidence.md").write_text("fixture evidence")
        self.call("finish", "prior", "--owner", "worker", "--commit", "HEAD", "--evidence", ".builder/evidence.md", "--checks", "fixture")
        self.call("review", "prior", "--reviewer", "reviewer", "--result", "accepted", "--note", "fixture verified")
        (self.root / "domain/a.txt").write_text("followup change")
        self.git("add", "domain"); self.git("commit", "-m", "followup base")
        self.add("followup", depends=["prior"])
        self.call("claim", "followup", "--owner", "next-worker")
        db = board.connect(self.db)
        prior = db.execute("SELECT status,commit_sha,reviewer,review_note FROM tasks WHERE id='prior'").fetchone()
        self.assertEqual(tuple(prior), ("accepted", prior_commit, "reviewer", "fixture verified"))
        self.assertEqual(db.execute("SELECT status FROM tasks WHERE id='followup'").fetchone()[0], "claimed")
        db.close()

    def test_accepted_dependency_requires_descendant_base(self):
        self.add("prior"); self.call("claim", "prior", "--owner", "worker")
        self.git("checkout", "-b", "auto/prior")
        (self.root / "domain/a.txt").write_text("prior change")
        self.git("add", "domain"); self.git("commit", "-m", "prior commit")
        (self.root / ".builder/evidence.md").write_text("fixture evidence")
        self.call("finish", "prior", "--owner", "worker", "--commit", "HEAD", "--evidence", ".builder/evidence.md", "--checks", "fixture")
        self.call("review", "prior", "--reviewer", "reviewer", "--result", "accepted", "--note", "fixture verified")
        prior_commit = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")
        self.add("followup", depends=["prior"], scope="domain",)
        with self.assertRaises(ValueError):
            self.call("claim", "followup", "--owner", "next-worker")
        self.assertEqual(self.git("rev-parse", "main"), self.git("rev-parse", "HEAD"))
        self.assertTrue(prior_commit != self.git("rev-parse", "main"))

    def test_accepted_overlap_without_explicit_dependency_is_rejected(self):
        self.add("prior"); self.call("claim", "prior", "--owner", "worker")
        self.git("checkout", "-b", "auto/prior")
        (self.root / "domain/a.txt").write_text("prior change")
        self.git("add", "domain"); self.git("commit", "-m", "prior commit")
        (self.root / ".builder/evidence.md").write_text("fixture evidence")
        self.call("finish", "prior", "--owner", "worker", "--commit", "HEAD", "--evidence", ".builder/evidence.md", "--checks", "fixture")
        self.call("review", "prior", "--reviewer", "reviewer", "--result", "accepted", "--note", "fixture verified")
        self.add("followup")
        with self.assertRaisesRegex(ValueError, "write scope overlaps task prior"):
            self.call("claim", "followup", "--owner", "next-worker")

    def test_active_overlaps_are_never_continuations(self):
        self.add("active"); self.call("claim", "active", "--owner", "worker")
        self.add("claimed-followup", depends=["active"])
        with self.assertRaises(ValueError):
            self.call("claim", "claimed-followup", "--owner", "next-worker")
        (self.root / "domain/a.txt").write_text("active task change")
        self.git("add", "domain"); self.git("commit", "-m", "active task commit")
        (self.root / ".builder/evidence.md").write_text("fixture evidence")
        self.call("finish", "active", "--owner", "worker", "--commit", "HEAD", "--evidence", ".builder/evidence.md", "--checks", "fixture")
        self.add("submitted-followup", depends=["active"])
        with self.assertRaises(ValueError):
            self.call("claim", "submitted-followup", "--owner", "next-worker")

    def test_worker_commit_cannot_claim_main_integration_before_merge(self):
        self.add("one"); self.call("claim", "one", "--owner", "worker")
        self.git("checkout", "-b", "auto/one")
        (self.root / "domain/a.txt").write_text("worker change")
        self.git("add", "domain"); self.git("commit", "-m", "worker commit")
        (self.root / ".builder/evidence.md").write_text("fixture evidence")
        self.call("finish", "one", "--owner", "worker", "--commit", "HEAD", "--evidence", ".builder/evidence.md", "--checks", "fixture")
        self.call("review", "one", "--reviewer", "reviewer", "--result", "accepted", "--note", "fixture verified")
        with self.assertRaises(ValueError):
            self.call("integrate", "one", "--commit", "HEAD")
        self.git("checkout", "main"); self.git("merge", "--ff-only", "auto/one")
        self.call("integrate", "one", "--commit", "HEAD")


if __name__ == "__main__":
    unittest.main()
