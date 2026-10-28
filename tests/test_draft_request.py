"""Тесты draft_request.py и проверок реестра запросов в audit_evidence.py.

Запуск: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PERSON = "{id},,,,,,,,,,,,,,,"
ARTIFACT = "{id},,title,url,,,,,,,P-0001,,,,direct,image-of-original,G1,clear,,,,,"


class DraftRequestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.case = Path(self.tmp.name) / "case"
        subprocess.run(
            [sys.executable, str(SCRIPTS / "init_case.py"), "--case-dir", str(self.case),
             "--research-question", "q"],
            check=True, capture_output=True,
        )
        with (self.case / "person-register.csv").open("a", encoding="utf-8") as handle:
            handle.write(PERSON.format(id="P-0001") + "\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "draft_request.py"), *args, "--case-dir", str(self.case)],
            capture_output=True, encoding="utf-8",
        )

    def rows(self) -> list[dict[str, str]]:
        with (self.case / "request-register.csv").open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def audit(self) -> dict:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "audit_evidence.py"), "--case-dir", str(self.case), "--json"],
            capture_output=True, encoding="utf-8",
        )
        return json.loads(proc.stdout)

    def test_init_creates_register_and_requests_dir(self):
        self.assertTrue((self.case / "request-register.csv").is_file())
        self.assertTrue((self.case / "requests").is_dir())
        self.assertEqual(self.audit()["counts"]["requests"], 0)

    def test_new_draft_creates_file_and_row(self):
        proc = self.run_script(
            "new", "--addressee", "Архив ЗАГС", "--channel", "portal", "--purpose", "Запись о смерти 1977",
            "--persons", "P-0001", "--service-url", "https://example.invalid/service",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["request_id"], "RQ-0001")
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["target_person_ids"], "P-0001")
        path = self.case / row["file"]
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("# RQ-0001: Запись о смерти 1977", text)
        self.assertIn("| Статус | draft |", text)
        self.assertIn("| Официальная страница | https://example.invalid/service |", text)
        self.assertNotIn("{", text)
        second = self.run_script("new", "--addressee", "Архив", "--channel", "email", "--purpose", "Второй")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.rows()[1]["request_id"], "RQ-0002")

    def test_unknown_person_or_channel_is_rejected(self):
        proc = self.run_script("new", "--addressee", "Архив", "--channel", "portal", "--purpose", "x", "--persons", "P-0009")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("P-0009", proc.stderr)
        proc = self.run_script("new", "--addressee", "Архив", "--channel", "fax", "--purpose", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.rows(), [])

    def test_update_tracks_status_and_audit_warns_on_unconfirmed_steps(self):
        self.run_script("new", "--addressee", "Архив", "--channel", "portal", "--purpose", "Справка")
        proc = self.run_script("update", "--request-id", "RQ-0001", "--status", "sent", "--date-sent", "2026-10-28")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self.rows()[0]
        self.assertEqual((row["status"], row["date_sent"]), ("sent", "2026-10-28"))
        text = (self.case / row["file"]).read_text(encoding="utf-8")
        self.assertIn("| Статус | sent |", text)
        self.assertIn("| Отправлено | 2026-10-28 |", text)
        self.assertIn(": sent", text.splitlines()[-1])
        warnings = self.audit()["warnings"]
        self.assertTrue(any("RQ-0001" in w and "разрешения пользователя" in w for w in warnings), warnings)

        proc = self.run_script("update", "--request-id", "RQ-0001", "--status", "registered",
                               "--authorization", "поручение в беседе 2026-10-28")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        warnings = self.audit()["warnings"]
        self.assertTrue(any("registered без номера обращения" in w for w in warnings), warnings)

        proc = self.run_script("update", "--request-id", "RQ-0001", "--status", "answered")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("answered", proc.stderr)

        with (self.case / "artifact-register.csv").open("a", encoding="utf-8") as handle:
            handle.write(ARTIFACT.format(id="A-0001") + "\n")
        proc = self.run_script("update", "--request-id", "RQ-0001", "--status", "answered",
                               "--reply-artifacts", "A-0001", "--tracking-number", "123")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self.rows()[0]
        self.assertEqual((row["status"], row["tracking_number"], row["reply_artifact_ids"]), ("answered", "123", "A-0001"))
        result = self.audit()
        self.assertEqual(result["errors"], [])
        self.assertFalse(any("RQ-0001" in w for w in result["warnings"]), result["warnings"])

    def test_update_rejects_unknown_request_and_bad_date(self):
        proc = self.run_script("update", "--request-id", "RQ-0009", "--status", "sent")
        self.assertEqual(proc.returncode, 2)
        self.run_script("new", "--addressee", "Архив", "--channel", "post", "--purpose", "x")
        proc = self.run_script("update", "--request-id", "RQ-0001", "--date-sent", "28.10.2026")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("YYYY-MM-DD", proc.stderr)

    def test_audit_reports_broken_references_in_register(self):
        with (self.case / "request-register.csv").open("a", encoding="utf-8") as handle:
            handle.write("RQ-0001,2026-10-28T00:00:00+00:00,mailed,Архив,email,,P-0007,x,C-0003,,,,,,A-0005,requests/missing.md,\n")
        result = self.audit()
        joined = "\n".join(result["errors"] + result["warnings"])
        for fragment in ("person_id P-0007", "claim_id C-0003", "artifact_id A-0005", "статус запроса 'mailed'", "requests/missing.md"):
            self.assertIn(fragment, joined)


if __name__ == "__main__":
    unittest.main()
