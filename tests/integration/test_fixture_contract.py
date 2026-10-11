"""Fast fixture provenance and disposable-harness checks; no services/secrets."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("auth_e2e", HERE / "auth_e2e.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

class FixtureContract(unittest.TestCase):
    def test_exact_tables_and_pinned_provenance(self):
        source = (HERE / "polaris-auth-schema.sql").read_text()
        origin = json.loads((HERE / "polaris-auth-origin.json").read_text())
        self.assertEqual(origin["commit"], "0101ce955069af1bcf1dd4299b03250679d9b1e1")
        self.assertNotIn("INSERT INTO", source.upper())
        self.assertNotIn("DROP TABLE", source.upper())
        self.assertEqual(re.findall(r"CREATE TABLE `([^`]+)`", source), ["users", "bans"])
        for table, info in origin["tables"].items():
            block = re.search(r"(?m)^CREATE TABLE `" + table + r"` \([\s\S]+?^\) ENGINE=[^\n]+;", source)[0] + "\n"
            self.assertEqual(hashlib.sha256(block.encode()).hexdigest(), info["sha256"])

    def test_java_synthetic_vectors(self):
        record = json.loads((HERE / "java-bcrypt-fixtures.json").read_text())
        self.assertEqual(record["provenance"]["polarisCommit"], "0101ce955069af1bcf1dd4299b03250679d9b1e1")
        rows = record["vectors"]
        self.assertEqual(len(rows),4)
        self.assertTrue(all(row["hash"].startswith(("$2a$", "$2y$")) for row in rows))
        self.assertEqual(len(next(row["password"] for row in rows if row["id"] == "ascii-72-bytes").encode()),72)

    def test_rfc6238_sha1_six_digit_counter(self):
        # Published RFC6238 synthetic SHA1 test secret; never an enrollment key.
        self.assertEqual(harness.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",1),"287082")

    def test_refuses_another_container_project(self):
        class Result:
            def __init__(self, stdout): self.stdout = stdout
        other = [{"Config":{"Labels":{"com.docker.compose.project":"prior-stopped-project","com.docker.compose.service":"mariadb"}}}]
        with patch.object(harness,"command",side_effect=[Result("fake-id"),Result(json.dumps(other))]):
            with self.assertRaisesRegex(RuntimeError,"outside the disposable auth project"):
                harness.container_info("mariadb")

    def test_refuses_another_worktree(self):
        class Result:
            def __init__(self, stdout): self.stdout = stdout
        other = [{"Config":{"Labels":{"com.docker.compose.project":harness.PROJECT,"com.docker.compose.service":"mariadb","com.docker.compose.project.working_dir":"/tmp/other-checkout"}}}]
        with patch.object(harness,"command",side_effect=[Result("fake-id"),Result(json.dumps(other))]):
            with self.assertRaisesRegex(RuntimeError,"outside this auth worktree"):
                harness.container_info("mariadb")

if __name__ == "__main__":
    unittest.main()
