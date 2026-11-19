import base64
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("scanner", Path(__file__).with_name("security.py"))
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def fixture():
    workspaces = ["packages/desktop", "packages/cli", "packages/server"]
    entries = {"": {"name": "root", "version": "1.0.0", "workspaces": workspaces}}
    for path in workspaces:
        name = path.replace("packages/", "@getpaseo/")
        entries[path] = {"name": name, "version": "1.0.0"}
        entries["node_modules/" + name] = {"link": True, "resolved": path}
    entries["packages/desktop"]["dependencies"] = {"@getpaseo/cli": "*", "@getpaseo/server": "*"}
    entries["node_modules/alias"] = {"name": "actual", "version": "1.2.3",
        "resolved": "https://registry.npmjs.org/actual/-/actual-1.2.3.tgz",
        "integrity": "sha512-" + base64.b64encode(bytes(64)).decode()}
    return {"lockfileVersion": 3, "packages": entries}


class SecurityTests(unittest.TestCase):
    def test_lock_validates_registry_identity_integrity_and_workspace_links(self):
        lock = fixture()
        self.assertEqual(scanner.public_packages(lock), [("actual", "1.2.3")])
        for loc, field, value in [
            ("node_modules/alias", "integrity", "sha512-AAAA"),
            ("node_modules/alias", "resolved", "https://evil.invalid/actual.tgz"),
            ("node_modules/alias", "name", "another"),
            ("node_modules/@getpaseo/cli", "resolved", "../outside"),
            ("packages/desktop", "dependencies", {"x": "file:../../outside"}),
        ]:
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(lock)
                bad["packages"][loc][field] = value
                with self.assertRaises(ValueError):
                    scanner.public_packages(bad)

    def test_osv_exact_version_alias_and_paginated_match(self):
        inventory = scanner.public_packages(fixture())
        result = {"packagesChecked": 0, "findings": []}
        def query(pins):
            self.assertEqual(pins[0]["package"], {"name": "actual", "ecosystem": "npm"})
            self.assertEqual(pins[0]["version"], "1.2.3")
            if "page_token" not in pins[0]:
                return {"results": [{"vulns": [{"id": "GHSA-abcd"}], "next_page_token": "next"}]}
            self.assertEqual(pins[0]["page_token"], "next")
            return {"results": [{"vulns": [{"id": "MAL-2026-1"}]}]}
        scanner.malware_scan(inventory, result, query)
        self.assertEqual(result["packagesChecked"], 1)
        self.assertEqual(result["findings"][0]["id"], "MAL-2026-1")

    def test_osv_rejects_error_partial_batches_and_repeated_tokens(self):
        for reply in [{"results": []}, {"results": [{"error": "bad package"}]},
                      {"results": [{"next_page_token": "same"}]}, {"results": [{"vulns": None}]}]:
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                scanner.malware_scan([("x", "1.0.0")], {"packagesChecked": 0, "findings": []}, lambda _: reply)

    def test_npm_distinguishes_blocking_findings_from_operational_failure(self):
        meta = dict.fromkeys(scanner.LEVELS, 0)
        meta.update(high=1, total=1)
        data = {"auditReportVersion": 2, "metadata": {"vulnerabilities": meta}, "vulnerabilities": {
            "actual": {"severity": "high", "nodes": ["node_modules/alias"], "via": [
                {"url": "https://github.com/advisories/GHSA-abcd-efgh-ijkl"},
                {"url": "https://evil.invalid/<b>"}]}}}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "package-lock.json").write_text(json.dumps(fixture()))
            proc = subprocess.CompletedProcess(["npm"], 1, json.dumps(data).encode(), b"")
            lines = []
            with patch.object(scanner.subprocess, "run", return_value=proc):
                self.assertTrue(scanner.npm_audit(directory, directory, "test", [], lines))
                self.assertIn("actual@1.2.3", str(lines))
                self.assertNotIn("evil.invalid", str(lines))
                proc.stdout = b'{"error": {"code": "E503"}}'
                with self.assertRaises(ValueError):
                    scanner.npm_audit(directory, directory, "test", [], [])


if __name__ == "__main__":
    unittest.main()
