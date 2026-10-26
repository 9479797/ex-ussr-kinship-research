"""Optional API workflow: synthetic responses only, no account or network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_source_search import FakeClient, SCRIPTS, group, raw_record

import familysearch as browser
import familysearch_api as api
import register_search
import research_common as common
from familysearch_records import compile_patterns


class DiscoveryTests(unittest.TestCase):
    def client(self, field="offset", repeated=False):
        def handler(path, body, as_json):
            if body["query"] == api.FIELDS_QUERY:
                return {"data": {"__type": {"inputFields": [{"name": field}] if field else []}}}
            query = body["variables"]["si"]
            second = query.get(field, 0) in (1, 2) if field else query["pageSize"] == 2
            gid = "G2" if second and not repeated else "G1"
            return {"data": {"search": {"totalCount": 2, "groups": [{"id": gid, "imageApids": [gid],
                       "imageGroupNumber": "000000001", "childCount": 1, "indexedChildCount": 1}]}}}
        return FakeClient(handler)

    def test_offset_and_one_based_page_discovery(self):
        for field, position in (("offset", 1), ("page", 2)):
            client = self.client(field)
            result = api.discover_books(client, {"place": 1, "pageSize": 1})
            self.assertEqual(result["total"], 2)
            self.assertEqual(client.calls[-1][1]["variables"]["si"][field], position)
            self.assertEqual(result["groups"][0]["dgs"], "000000001")

    def test_expanded_page_and_capped_response(self):
        self.assertTrue(api.discover_books(self.client(None), {"pageSize": 1})["complete"])
        with self.assertRaises(common.SourceError):
            api.discover_books(self.client(repeated=True), {"pageSize": 1})

    def test_changed_metadata_and_totals_are_rejected(self):
        client = self.client()
        original = client.handler
        for change in ("total", "metadata"):
            def handler(path, body, as_json):
                result = original(path, body, as_json)
                if body["query"] != api.FIELDS_QUERY and body["variables"]["si"].get("offset"):
                    page = result["data"]["search"]
                    if change == "total":
                        page["totalCount"] = 3
                    else:
                        page["groups"][0]["id"] = "G1"
                return result
            with self.assertRaises(common.SourceError):
                api.discover_books(FakeClient(handler), {"pageSize": 1})

    def test_empty_frame_requires_verified_session_once(self):
        client = FakeClient(lambda *args: {})
        session = FakeClient(lambda *args: {"users": [{"id": "synthetic-user"}]})
        reader = api.FrameReader(client, session)
        self.assertTrue(reader.read("A")["empty_response"])
        reader.read("B")
        self.assertEqual(len(session.calls), 1)
        with self.assertRaises(common.SourceError):
            api.FrameReader(client, FakeClient(lambda *args: {"users": []})).read("C")


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_two_phases_keep_full_raw_and_reuse_successful_records(self):
        raw = raw_record()
        raw["futureField"] = {"must": "survive normalization"}
        client = FakeClient(lambda path, *args: {"sourceDescriptions": [{"id": "R"}]} if "/images/" in path else raw)
        phase_a = api.harvest([group()], self.directory, "test", client, api.FrameReader(client, client), phase="a")
        self.assertTrue(phase_a["phase_complete"])
        self.assertFalse(phase_a["complete"])
        self.assertEqual(phase_a["missing_records"], ["R"])
        phase_b = api.harvest([group()], self.directory, "test", client, api.FrameReader(client, client), phase="b")
        self.assertTrue(phase_b["complete"])
        cached = common.read_json(self.directory / "fsk_test_records.json")["R"]
        self.assertEqual(cached["raw"], raw)
        no_requests = FakeClient(lambda *args: self.fail("completed harvest made a source request"))
        again = api.harvest([group()], self.directory, "test", no_requests, api.FrameReader(no_requests, no_requests))
        self.assertTrue(again["complete"])
        self.assertEqual(common.read_json(self.directory / "fsk_test_records.json")["R"]["captured_at"], cached["captured_at"])

    def test_resume_retries_failed_frame_without_refetching_success(self):
        def first(path, *args):
            if "/images/A/" in path:
                return {"sourceDescriptions": [{"id": "R"}]}
            if "/images/B/" in path:
                raise common.SourceError("temporary failure", 503)
            return raw_record()
        client = FakeClient(first)
        result = api.harvest([group(("A", "B"))], self.directory, "test", client, api.FrameReader(client, client))
        self.assertIn("G|2", result["failed_frames"])
        second = FakeClient(lambda *args: {"sourceDescriptions": []})
        result = api.harvest([group(("A", "B"))], self.directory, "test", second, api.FrameReader(second, second))
        self.assertTrue(result["complete"])
        self.assertEqual(len(second.calls), 1)

    def test_access_stops_requests_and_saves_checkpoint(self):
        for status in (401, 403, 429):
            def denied(*args):
                raise common.SourceError("access stopped", status)
            client = FakeClient(denied)
            result = api.harvest([group(("A", "B"))], self.directory, str(status), client, api.FrameReader(client, client))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(result["blocked"]["status"], status)
            self.assertFalse(result["complete"])
            self.assertTrue((self.directory / f"fsk_{status}_frames.json").exists())
            self.assertFalse((self.directory / f"fsk_{status}.lock").exists())

    def test_record_failure_resumes_phase_b_and_preserves_all_frames(self):
        def denied_record(path, *args):
            if "/images/" in path:
                return {"sourceDescriptions": [{"id": "R"}]}
            raise common.SourceError("access stopped", 403)
        client = FakeClient(denied_record)
        partial = api.harvest([group()], self.directory, "test", client, api.FrameReader(client, client))
        self.assertEqual(partial["successful_frames"], 1)
        self.assertEqual(partial["blocked"]["status"], 403)
        client = FakeClient(lambda path, *args: raw_record())
        complete = api.harvest([group()], self.directory, "test", client, api.FrameReader(client, client), phase="b")
        self.assertTrue(complete["complete"])
        self.assertEqual(len(client.calls), 1)

    def test_raw_upgrade_empty_refresh_and_frame_identity(self):
        client = FakeClient(lambda path, *args: {"sourceDescriptions": [{"id": "R"}]} if "/images/" in path else raw_record())
        api.harvest([group()], self.directory, "test", client, api.FrameReader(client, client), retain_raw=False)
        before = len(client.calls)
        api.harvest([group()], self.directory, "test", client, api.FrameReader(client, client))
        self.assertEqual(len(client.calls) - before, 1)
        self.assertIn("raw", common.read_json(self.directory / "fsk_test_records.json")["R"])
        with self.assertRaises(common.SourceError):
            api.harvest([group(("B",))], self.directory, "test", client, api.FrameReader(client, client))
        empty = FakeClient(lambda *args: {"sourceDescriptions": []})
        api.harvest([group()], self.directory, "empty", empty, api.FrameReader(empty, empty))
        api.harvest([group()], self.directory, "empty", empty, api.FrameReader(empty, empty), refresh_empty=True)
        self.assertEqual(len(empty.calls), 2)

    def test_end_to_end_harvest_filter_register_is_evidence_first(self):
        client = FakeClient(lambda path, *args: {"sourceDescriptions": [{"id": "R"}]} if "/images/" in path else raw_record())
        api.harvest([group()], self.directory, "test", client, api.FrameReader(client, client))
        groups = self.directory / "groups.json"
        common.save_json(groups, {"groups": [group()], "total": 1})
        result = browser.filter_harvests(["test"], self.directory, [groups], compile_patterns(surnames=["Сидоров"]))
        self.assertEqual(result["hits"]["Сидоров"][0]["pid"], "witness")
        self.assertEqual(result["candidates"][0]["record"]["raw"], raw_record())
        analysis = self.directory / "analysis.json"
        common.save_json(analysis, result)
        case = self.directory / "case"
        initialized = subprocess.run([sys.executable, str(SCRIPTS / "init_case.py"), "--case-dir", str(case), "--research-question", "Проверить свидетеля"], capture_output=True, text=True)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        protected = {name: (case / name).read_bytes() for name in ("person-register.csv", "claims-register.csv", "relationship-register.csv")}
        self.assertEqual(register_search.register(case, analysis, "Проверить свидетеля")["added_artifacts"], 1)
        self.assertEqual(register_search.register(case, analysis, "Проверить свидетеля")["added_artifacts"], 0)
        self.assertTrue(all((case / name).read_bytes() == content for name, content in protected.items()))
        audit = subprocess.run([sys.executable, str(SCRIPTS / "audit_evidence.py"), "--case-dir", str(case), "--json"], capture_output=True, text=True)
        self.assertEqual(audit.returncode, 0)
        self.assertEqual(json.loads(audit.stdout)["errors"], [])


class InterfaceTests(unittest.TestCase):
    def test_session_check_outputs_no_profile_or_credentials(self):
        profile = {"users": [{"id": "private-account", "email": "private@example.invalid"}]}
        client = FakeClient(lambda *args: profile)
        result = api.check_session(client)
        self.assertTrue(result["session_valid"])
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(client.calls[0][0], "/platform/users/current.json")
        with self.assertRaises(common.SourceError):
            api.check_session(FakeClient(lambda *args: {"users": []}))

    def test_cli_places_keeps_unicode_query_and_bounded_search_keeps_full_records(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            client = FakeClient(lambda *args: {"places": [{"id": "test-place"}]})
            args = api.parser().parse_args(["places", "Село Ивановка", "--out", str(output)])
            with patch.object(api, "load_token", return_value="synthetic-secret"), patch.object(api, "SourceClient", return_value=client):
                self.assertEqual(api.run(args), 0)
            self.assertIn("%D0", client.calls[0][0])
            self.assertEqual(common.read_json(output)["query"], "Село Ивановка")
            client = FakeClient(lambda path, *args: {"entries": []} if "offset=75" in path else
                                {"entries": [{"id": "R", "content": {"gedcomx": raw_record()}}]})
            args = api.parser().parse_args(["search", "--surname", "Иванов", "--out", str(output)])
            with patch.object(api, "load_token", return_value="synthetic-secret"), patch.object(api, "SourceClient", return_value=client):
                self.assertEqual(api.run(args), 0)
            result = common.read_json(output)
            self.assertEqual(result["records"][0]["raw"], raw_record())
            self.assertTrue(result["search_exhausted"])
            self.assertFalse(result["coverage"]["complete"])
            self.assertNotIn("synthetic-secret", output.read_text())

    def test_cli_denied_harvest_writes_partial_report_and_returns_one(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            groups = directory / "groups.json"
            output = directory / "coverage.json"
            common.save_json(groups, {"total": 1, "groups": [group()]})
            def denied(*args):
                raise common.SourceError("access stopped", 403)
            client = FakeClient(denied)
            args = api.parser().parse_args(["harvest", str(groups), "--dir", str(directory), "--tag", "test", "--out", str(output)])
            with patch.object(api, "load_token", return_value="synthetic-secret"), patch.object(api, "SourceClient", return_value=client):
                self.assertEqual(api.run(args), 1)
            self.assertEqual(common.read_json(output)["blocked"]["status"], 403)
            self.assertFalse(common.read_json(output)["complete"])
            self.assertNotIn("synthetic-secret", output.read_text())

    def test_api_is_optional_and_main_browser_cli_never_loads_credentials(self):
        args = api.parser().parse_args(["harvest", "groups.json", "--dir", "/tmp", "--tag", "test"])
        self.assertTrue(args.raw)
        help_result = subprocess.run([sys.executable, str(SCRIPTS / "familysearch_api.py"), "--help"], capture_output=True, text=True,
                                     env={**os.environ, "FS_TOKEN": ""})
        self.assertEqual(help_result.returncode, 0)
        with tempfile.TemporaryDirectory() as directory:
            book = Path(directory) / "book.json"
            common.save_json(book, {"book": {"group_id": "G", "expected_frames": 1}})
            with patch.object(api, "load_token", side_effect=AssertionError("browser requested separate auth")):
                args = browser.parser().parse_args(["harvest", str(book), "--tag", "test", "--dir", directory, "--out", str(Path(directory)/"plan.json")])
                self.assertEqual(browser.run(args), 0)

    def test_token_is_supplied_separately_and_not_accepted_as_cli_value(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "session"
            token_file.write_text("synthetic-secret\n")
            self.assertEqual(api.load_token(token_file), "synthetic-secret")
            token_file.write_text("fssessionid=synthetic-secret\n")
            self.assertEqual(api.load_token(token_file), "synthetic-secret")
            token_file.write_text("fssessionid=synthetic-secret; other=not-accepted")
            with self.assertRaises(common.SourceError):
                api.load_token(token_file)
        with patch.dict(os.environ, {"FS_TOKEN": ""}):
            with self.assertRaises(common.SourceError):
                api.load_token()
        with self.assertRaises(common.SourceError):
            api.load_token(SCRIPTS / "session")
        result = subprocess.run([sys.executable, str(SCRIPTS / "familysearch_api.py"), "books", "--dgs", "0001", "--token", "synthetic-secret"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
