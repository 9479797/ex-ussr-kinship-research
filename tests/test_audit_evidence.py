"""Тесты audit_evidence.py на временных делах, созданных init_case.py.

Запуск: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

ARTIFACT = "{id},,title,url,,,,,,,P-0001,,,,direct,image-of-original,{group},clear,,,{claims},,"
CLAIM = "{id},text,P-0001,birth,{artifacts},{groups},,{status},moderate,rationale,"
PERSON = "{id},,,,,,,,,,,,,,,"
RELATION = "{id},P-0001,parent-of,P-0002,biological,{claims},{artifacts},,{groups},{status},moderate,rationale"


class AuditCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.case = Path(self.tmp.name) / "case"
        subprocess.run(
            [sys.executable, str(SCRIPTS / "init_case.py"), "--case-dir", str(self.case),
             "--research-question", "q"],
            check=True, capture_output=True,
        )
        self.append("person-register.csv", PERSON.format(id="P-0001"), PERSON.format(id="P-0002"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def append(self, name: str, *lines: str) -> None:
        with (self.case / name).open("a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))

    def audit(self) -> dict:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "audit_evidence.py"), "--case-dir", str(self.case), "--json"],
            capture_output=True, text=True,
        )
        return json.loads(proc.stdout)

    def two_artifacts(self, second_group: str = "G2") -> None:
        self.append(
            "artifact-register.csv",
            ARTIFACT.format(id="A-0001", group="G1", claims="C-0001"),
            ARTIFACT.format(id="A-0002", group=second_group, claims="C-0001"),
        )

    def test_fresh_case_is_clean(self):
        # Плейсхолдеры шаблона отчёта (A-...., R-0001 при пустых реестрах) не дают ошибок.
        result = self.audit()
        self.assertEqual(result["errors"], [])

    def test_declared_group_without_artifact_is_error(self):
        self.append("artifact-register.csv", ARTIFACT.format(id="A-0001", group="G1", claims="C-0001"))
        self.append("claims-register.csv",
                    CLAIM.format(id="C-0001", artifacts="A-0001", groups="G1;G2", status="corroborated"))
        result = self.audit()
        self.assertTrue(any("C-0001: independence_group G2" in e for e in result["errors"]))
        self.assertTrue(any("C-0001: статус corroborated" in w for w in result["warnings"]))

    def test_backed_groups_corroborate(self):
        self.two_artifacts()
        self.append("claims-register.csv",
                    CLAIM.format(id="C-0001", artifacts="A-0001;A-0002", groups="G1;G2", status="corroborated"))
        result = self.audit()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])

    def test_copies_share_group(self):
        self.two_artifacts(second_group="G1")
        self.append("claims-register.csv",
                    CLAIM.format(id="C-0001", artifacts="A-0001;A-0002", groups="G1", status="corroborated"))
        result = self.audit()
        self.assertTrue(any("найдено независимых групп: 1" in w for w in result["warnings"]))

    def test_relationship_groups_come_from_claims(self):
        self.two_artifacts()
        self.append("claims-register.csv",
                    CLAIM.format(id="C-0001", artifacts="A-0001;A-0002", groups="", status="corroborated"))
        self.append("relationship-register.csv",
                    RELATION.format(id="R-0001", claims="C-0001", artifacts="", groups="G1;G2",
                                    status="corroborated"),
                    RELATION.format(id="R-0002", claims="", artifacts="A-0001", groups="G1;G9",
                                    status="corroborated"))
        result = self.audit()
        self.assertFalse(any(e.startswith("R-0001") for e in result["errors"]))
        self.assertTrue(any("R-0002: independence_group G9" in e for e in result["errors"]))

    def test_report_unknown_ids_are_errors(self):
        self.two_artifacts()
        self.append("claims-register.csv",
                    CLAIM.format(id="C-0001", artifacts="A-0001", groups="", status="single-source"))
        self.append("final-report.md", "", "Вывод [A-0001; A-0099], см. C-0042.")
        errors = self.audit()["errors"]
        self.assertTrue(any("A-0099" in e for e in errors))
        self.assertTrue(any("C-0042" in e for e in errors))
        self.assertFalse(any("A-0001" in e for e in errors))

    def test_report_status_must_match_register(self):
        self.two_artifacts()
        self.append("claims-register.csv",
                    CLAIM.format(id="C-0001", artifacts="A-0001", groups="", status="single-source"))
        self.append("relationship-register.csv",
                    RELATION.format(id="R-0001", claims="C-0001", artifacts="", groups="",
                                    status="single-source"))
        self.append("final-report.md", "", "### R-0001: P-0001 — отец — P-0002", "",
                    "**Статус:** `corroborated`", "",
                    "| C-0001 | текст | corroborated | A-0001 |  |  |")
        errors = self.audit()["errors"]
        self.assertTrue(any("R-0001 — в отчёте corroborated, в реестре single-source" in e for e in errors))
        self.assertTrue(any("C-0001 — в отчёте corroborated, в реестре single-source" in e for e in errors))

    def test_summary_counts_open_items(self):
        self.two_artifacts()
        self.append("claims-register.csv",
                    CLAIM.format(id="C-0001", artifacts="A-0001", groups="", status="hypothesis"))
        self.append("final-report.md", "Фамилия [неразборчиво], отец [предположительно: Пётр].")
        summary = self.audit()["summary"]
        self.assertEqual(summary["claims_by_status"], {"hypothesis": 1})
        self.assertEqual(summary["report_markers"], {"неразборчиво": 1, "предположительно": 1})


if __name__ == "__main__":
    unittest.main()
