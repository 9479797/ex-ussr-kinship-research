"""Synthetic source-search tests; no live accounts, network or private case data."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import familysearch as fs
import gwar
import register_search
import research_common as common
from familysearch_records import analyze, classify, compile_patterns, coverage, parse_record


def raw_record():
    return {
        "persons": [
            {"id": "child", "principal": True,
             "names": [{"nameForms": [{"fullText": "Иван Иванов"}, {"fullText": "Ivan Ivanov"}]}],
             "identifiers": {"http://gedcomx.org/Persistent": ["https://www.familysearch.org/ark:/61903/1:1:TEST-CHILD"]},
             "facts": [{"type": "http://gedcomx.org/Birth", "date": {"original": "14 января 1885", "formal": "+1885-01-14"},
                        "place": {"original": "Условное село"}}]},
            {"id": "parent", "names": [{"nameForms": [{"fullText": "Мария Петрова"}]}]},
            {"id": "witness", "names": [{"nameForms": [{"fullText": "Пётр Сидоров"}, {"fullText": "Petr Sidorov"}]}]},
        ],
        "relationships": [{"type": "http://gedcomx.org/ParentChild", "person1": {"resourceId": "parent"}, "person2": {"resourceId": "child"}}],
        "fields": [{"type": "http://example.org/RecordId", "values": [{"text": "000000001_1_1"}]}],
        "sourceDescriptions": [{"about": "https://www.familysearch.org/ark:/61903/3:1:TEST-IMAGE"}],
    }


def group(apids=("A",)):
    return {"id": "G", "title": "Условная книга", "dgs": "000000001", "apids": list(apids),
            "years": [1880, 1890], "places": ["Условное село"]}


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, path, body=None, as_json=True):
        self.calls.append((path, body, as_json))
        return self.handler(path, body, as_json)

    def redact(self, value):
        return str(value)


def hit(number):
    return {"_index": "gwar", "_type": "chelovek_donesenie", "_id": str(number),
            "_source": {"last_name": "Иванов", "first_name": "Пётр", "birth_place": "Условное село",
                        "documents_pages": {"pages_id": [1000 + number]}}}


class RecordTests(unittest.TestCase):
    def test_alternate_names_and_nonprincipal_people_are_searchable(self):
        record = parse_record(raw_record())
        result = analyze({"G|1": {"apid": "A", "rids": ["R"]}}, {"R": record}, {}, ["G|1"],
                         compile_patterns({"surnames": {"latin": "Sidorov", "cyrillic": r"\bПетрова\b"}}))
        self.assertEqual(result["hits"]["latin"][0]["pid"], "witness")
        self.assertFalse(result["hits"]["latin"][0]["principal"])
        self.assertEqual(result["hits"]["cyrillic"][0]["pid"], "parent")
        self.assertEqual(result["registry"][0]["parents"], ["Мария Петрова"])
        self.assertEqual(result["registry"][0]["mother"], "")
        self.assertEqual(result["registry"][0]["father"], "")
        self.assertEqual(len(result["candidates"]), 1)

    def test_couple_marriage_fact_is_not_lost_or_invented(self):
        raw = raw_record()
        raw["relationships"].append({"type": "http://gedcomx.org/Couple", "person1": {"resourceId": "child"},
                                     "person2": {"resourceId": "witness"}, "facts": [{"type": "http://gedcomx.org/Marriage",
                                     "date": {"original": "1905", "formal": "+1905"}}]})
        record = parse_record(raw)
        self.assertEqual(classify(record, "child")["date"], "1905")
        self.assertEqual(classify(record, "child")["ev"], "Брак")
        raw["relationships"][-1]["facts"] = []
        self.assertEqual(classify(parse_record(raw), "child")["ev"], "Рождение")
        with self.assertRaises(common.SourceError):
            compile_patterns({"inferMarriageFromBirthYear": True})

    def test_malformed_records_and_javascript_patterns_fail(self):
        for value in ({}, {"persons": []}, {"persons": [{"id": "x"}, {"id": "x"}]}):
            with self.assertRaises(common.SourceError):
                parse_record(value)
        with self.assertRaises(common.SourceError):
            compile_patterns(surnames=[r"\p{L}"])

    def test_coverage_separates_empty_failed_missing_and_record_errors(self):
        frames = {"G|1": {"rids": []}, "G|2": {"rids": [], "err": "denied"},
                  "G|3": {"rids": ["R", "missing"]}}
        report = coverage(frames, {"R": {"err": "bad record"}}, ["G|1", "G|2", "G|3", "G|4"])
        self.assertEqual(report["empty_frames"], {"G": [1]})
        self.assertEqual(report["missing_frames"], ["G|4"])
        self.assertEqual(report["missing_records"], ["missing"])
        self.assertEqual(report["failed_records"], {"R": "bad record"})
        self.assertFalse(report["complete"])


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.book = {"group_id": "G", "expected_frames": 2, "dgs": "000000001"}
        self.description = self.directory / "book.json"
        common.save_json(self.description, {"book": self.book})

    def batch(self, frames):
        path = self.directory / "batch.json"
        common.save_json(path, {"source": "familysearch-browser-book", "book": self.book, "frames": frames})
        return path

    def frame(self, n, status="indexed"):
        return {"number": n, "url": f"https://www.familysearch.org/ark:/61903/3:1:TEST-{n}",
                "captured_at": "2026-01-01T00:00:00Z", "status": status,
                "rows": [{"entry": 1, "name": "Иван Иванов", "text": "Иван Иванов"}] if status == "indexed" else []}

    def test_browser_plan_persists_and_resumes_without_any_source_client(self):
        # Even an accidentally configured environment session must be irrelevant.
        with patch.object(common.SourceClient, "request", side_effect=AssertionError("source request")):
            initial = fs.browser_harvest(self.description, self.directory, "test")
            self.assertEqual(initial["next_frames"], [1, 2])
            partial = fs.browser_harvest(self.description, self.directory, "test", self.batch([self.frame(1)]))
            self.assertEqual(partial["pending_frames"], [2])
            resumed = fs.browser_harvest(self.description, self.directory, "test")
            self.assertEqual(resumed["pending_frames"], [2])
            complete = fs.browser_harvest(self.description, self.directory, "test", self.batch([self.frame(2, "not-indexed")]))
            self.assertTrue(complete["coverage"]["complete"])
            self.assertEqual(complete["coverage"]["books"]["G"]["not_indexed_frames"], [2])
            result = fs.browser_import([complete["checkpoint"]], compile_patterns(surnames=["Иванов"]))
            self.assertEqual(len(result["candidates"]), 1)
            again = fs.browser_harvest(self.description, self.directory, "test", self.batch([self.frame(1)]))
            self.assertEqual(again["pending_frames"], [])

    def test_failed_frames_are_pending_and_can_be_replaced(self):
        partial = fs.browser_harvest(self.description, self.directory, "test", self.batch([self.frame(1, "failed")]))
        self.assertEqual(partial["pending_frames"], [1, 2])
        fixed = fs.browser_harvest(self.description, self.directory, "test", self.batch([self.frame(1)]))
        self.assertEqual(fixed["pending_frames"], [2])

    def test_conflicts_wrong_books_and_loading_do_not_overwrite_checkpoint(self):
        initial = fs.browser_harvest(self.description, self.directory, "test", self.batch([self.frame(1)]))
        path = Path(initial["checkpoint"])
        before = path.read_bytes()
        wrong = self.frame(1)
        wrong["rows"][0]["text"] = "Другая запись"
        loading = self.frame(2)
        loading["loading"] = True
        other_group = self.frame(2)
        other_group["url"] += "?groupId=OTHER"
        for frame in (wrong, loading, other_group):
            with self.assertRaises(common.SourceError):
                fs.browser_harvest(self.description, self.directory, "test", self.batch([frame]))
            self.assertEqual(path.read_bytes(), before)
        self.book["group_id"] = "OTHER"
        common.save_json(self.description, {"book": self.book})
        with self.assertRaises(common.SourceError):
            fs.browser_harvest(self.description, self.directory, "test")
        self.assertEqual(path.read_bytes(), before)

    def test_filter_finds_wholly_missing_frames_and_refuses_conflicts(self):
        common.save_json(self.directory / "fsk_test_frames.json", {"G|1": {"apid": "A", "rids": ["R"]}})
        common.save_json(self.directory / "fsk_test_records.json", {"R": parse_record(raw_record())})
        groups = self.directory / "groups.json"
        common.save_json(groups, {"groups": [group(("A", "B"))], "total": 1})
        result = fs.filter_harvests(["test"], self.directory, [groups], compile_patterns(surnames=["Сидоров"]))
        self.assertEqual(result["coverage"]["missing_frames"], ["G|2"])
        self.assertEqual(len(result["candidates"]), 1)
        with self.assertRaises(common.SourceError):
            fs.merge_entries({"R": {"persons": {"a": {}}}}, {"R": {"persons": {"b": {}}}}, "record")


class GwarTests(unittest.TestCase):
    def test_completed_cache_keeps_capture_time_without_new_requests(self):
        def handler(path, body, as_json):
            return "public page" if not as_json else {"hits": {"total": 1, "hits": [hit(1)]}}
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "gwar.json"
            first = gwar.collect(FakeClient(handler), {"last_name": "Иванов"}, list(gwar.KINDS.values()), 1, checkpoint)
            first["captured_at"] = "2000-01-01T00:00:00+00:00"
            common.save_json(checkpoint, first)
            def unexpected(*args):
                self.fail("A completed cache must not make a new source request")
            cached = gwar.collect(FakeClient(unexpected), {"last_name": "Иванов"}, list(gwar.KINDS.values()), 1, checkpoint)
            self.assertEqual(cached["captured_at"], first["captured_at"])
            self.assertEqual(cached["hits"], first["hits"])

    def test_limit_and_resume_preserve_all_entity_kinds_and_offsets(self):
        def handler(path, body, as_json):
            if not as_json:
                return "public page"
            start, size = body["from"], body["size"]
            return {"hits": {"total": {"value": 3, "relation": "eq"}, "hits": [hit(i) for i in range(start, min(3, start + size))]}}
        client = FakeClient(handler)
        entities = list(gwar.KINDS.values())
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "gwar.json"
            first = gwar.collect(client, {"birth_place": "Условное село"}, entities, 2, checkpoint)
            self.assertFalse(first["coverage"]["complete"])
            self.assertEqual(first["coverage"]["reason"], "limit-reached")
            second = gwar.collect(client, {"birth_place": "Условное село"}, entities, 3, checkpoint)
            self.assertTrue(second["coverage"]["complete"])
            self.assertEqual(len(second["hits"]), 3)
            self.assertEqual(client.calls[-1][1]["from"], 2)
            self.assertEqual(client.calls[-1][1]["entities"], entities)
            with self.assertRaises(common.SourceError):
                gwar.collect(client, {"birth_place": "Другая волость"}, entities, 3, checkpoint)

    def test_repeated_pages_and_access_failures_are_not_complete(self):
        client = FakeClient(lambda path, body, as_json: "page" if not as_json else {"hits": {"total": 3, "hits": [hit(0)]}})
        self.assertFalse(gwar.collect(client, {"last_name": "Иванов"}, list(gwar.KINDS.values()), 3)["coverage"]["complete"])
        def denied(*args):
            raise common.SourceError("limit", 429)
        blocked = gwar.collect(FakeClient(denied), {}, [], 3)
        self.assertEqual(blocked["blocked"]["status"], 429)

    def test_scan_locator_and_scoped_certificates(self):
        parsed = gwar.parse_scan('<page image="folder/test.jpg"><s id="row" c="1,2,3,4"/></page>')
        self.assertEqual(parsed["zones"][0]["c"], "1,2,3,4")
        self.assertEqual(parsed["image_url"], "https://gwar.mil.ru/cdn/imagesfww/folder/test.jpg")
        self.assertEqual(gwar.source_client().base, gwar.BASE)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.case = self.directory / "case"
        subprocess.run([sys.executable, str(SCRIPTS / "init_case.py"), "--case-dir", str(self.case),
                        "--research-question", "Кто родители условного человека?"], check=True, capture_output=True)

    def rows(self, name):
        with (self.case / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def result(self):
        result = analyze({"G|1": {"apid": "A", "rids": ["R"]}}, {"R": parse_record(raw_record())},
                         {"G": {"dgs": "000000001"}}, ["G|1"], compile_patterns(surnames=["Петрова"]))
        result["query"] = {"surnames": "Петрова"}
        return result

    def test_end_to_end_preserves_evidence_boundary_and_is_idempotent(self):
        path = self.directory / "analysis.json"
        common.save_json(path, self.result())
        untouched = {name: (self.case / name).read_bytes() for name in ("claims-register.csv", "person-register.csv", "relationship-register.csv")}
        first = register_search.register(self.case, path, "Найти родителей")
        second = register_search.register(self.case, path, "Найти родителей")
        self.assertEqual(first["added_artifacts"], 1)
        self.assertEqual(second["added_artifacts"], 0)
        self.assertFalse(second["added_search_log"])
        artifact = self.rows("artifact-register.csv")[0]
        self.assertEqual(artifact["originality"], "index")
        self.assertEqual(artifact["directness"], "lead-only")
        self.assertEqual(artifact["independence_group"], "")
        self.assertEqual(artifact["person_ids"], "")
        self.assertIn("DGS", artifact["archival_reference"].upper())
        snapshot = next((self.case / "sources" / "familysearch").glob("*.json"))
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), artifact["sha256"])
        for name, content in untouched.items():
            self.assertEqual((self.case / name).read_bytes(), content)
        audit = subprocess.run([sys.executable, str(SCRIPTS / "audit_evidence.py"), "--case-dir", str(self.case), "--json"], capture_output=True, text=True)
        self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
        self.assertIn("independence_group", audit.stdout)

    def test_preserves_manual_evaluation_and_source_revisions(self):
        path = self.directory / "analysis.json"
        result = self.result()
        common.save_json(path, result)
        register_search.register(self.case, path, "goal")
        fields, rows = register_search.read_register(self.case / "artifact-register.csv", "artifact_id")
        rows[0]["independence_group"] = "ORIGINAL-ACT-1"
        register_search.save_register(self.case / "artifact-register.csv", fields, rows)
        register_search.register(self.case, path, "goal")
        self.assertEqual(self.rows("artifact-register.csv")[0]["independence_group"], "ORIGINAL-ACT-1")
        result["candidates"][0]["record"]["persons"]["child"]["name"] = "Исправленное имя"
        common.save_json(path, result)
        self.assertEqual(register_search.register(self.case, path, "goal")["added_artifacts"], 1)
        self.assertEqual(len(self.rows("artifact-register.csv")), 2)

    def test_partial_empty_result_is_not_negative_evidence(self):
        result = self.result()
        result["candidates"] = []
        result["coverage"]["complete"] = False
        path = self.directory / "partial.json"
        common.save_json(path, result)
        report = register_search.register(self.case, path, "goal")
        self.assertEqual(report["result_status"], "partial")
        self.assertEqual(self.rows("artifact-register.csv"), [])
        self.assertEqual(self.rows("search-log.csv")[0]["result_status"], "partial")

    def test_gwar_import_and_restart_between_register_writes(self):
        path = self.directory / "gwar.json"
        common.save_json(path, {"source": "gwar", "hits": [hit(1), hit(2)], "query": {"birth_place": "Условное село"},
                                "coverage": {"complete": True}, "captured_at": common.timestamp()})
        original_save = register_search.save_register
        def fail_log(path, fields, rows):
            if path.name == "search-log.csv":
                raise OSError("simulated interruption")
            original_save(path, fields, rows)
        with patch.object(register_search, "save_register", side_effect=fail_log):
            with self.assertRaises(OSError):
                register_search.register(self.case, path, "goal")
        report = register_search.register(self.case, path, "goal")
        self.assertEqual(report["added_artifacts"], 0)
        self.assertEqual(len(self.rows("artifact-register.csv")), 2)
        self.assertEqual(len(self.rows("search-log.csv")), 1)

    def test_gwar_search_score_changes_do_not_duplicate_source_cards(self):
        path = self.directory / "gwar.json"
        record = {**hit(1), "_score": 150.0}
        data = {"source": "gwar", "hits": [record], "coverage": {"complete": True}, "query": {"birth_place": "Условное село"}}
        common.save_json(path, data)
        register_search.register(self.case, path, "goal")
        # Simulate a register written before search-ranking fields were excluded.
        fields, rows = register_search.read_register(self.case / "artifact-register.csv", "artifact_id")
        rows[0]["access_notes"] = re.sub(r"source_key=[a-f0-9]+", "source_key=" + "0" * 64, rows[0]["access_notes"])
        rows[0]["independence_group"] = "REVIEWED-DOCUMENT"
        register_search.save_register(self.case / "artifact-register.csv", fields, rows)
        record["_score"] = 95.0
        data["query"] = {"last_name": "Иванов"}
        common.save_json(path, data)
        second = register_search.register(self.case, path, "goal")
        self.assertEqual(second["added_artifacts"], 0)
        self.assertTrue(second["added_search_log"])
        self.assertEqual(len(self.rows("artifact-register.csv")), 1)
        self.assertEqual(self.rows("artifact-register.csv")[0]["independence_group"], "REVIEWED-DOCUMENT")
        record["_source"]["first_name"] = "Исправленное имя"
        common.save_json(path, data)
        self.assertEqual(register_search.register(self.case, path, "goal")["added_artifacts"], 1)

    def test_browser_index_text_uses_same_registers_without_inventing_parent_role(self):
        page = self.directory / "browser.json"
        snapshot = {"url": "https://www.familysearch.org/en/search/record/results?q.surname=test",
                    "scope": {"description": "one observed result", "page": 1},
                    "rows": [{"name": "Иван Иванов", "url": "https://www.familysearch.org/ark:/61903/1:1:TEST-ROW",
                              "text": "Иван Иванов. Parents: Мария Петрова, Пётр Иванов"}]}
        common.save_json(page, snapshot)
        result = fs.browser_import([page, page], compile_patterns(surnames=["Петрова"]))
        self.assertEqual(len(result["candidates"]), 1)
        self.assertIsNone(result["hits"]["Петрова"][0]["pid"])
        self.assertIsNone(result["hits"]["Петрова"][0]["principal"])
        self.assertFalse(result["coverage"]["complete"])
        record = result["candidates"][0]["record"]
        self.assertEqual(record["rels"], [])
        self.assertIn("Мария Петрова", record["browser_text"])
        self.assertEqual(record["persons"]["principal"]["facts"], [])
        output = self.directory / "browser-analysis.json"
        common.save_json(output, result)
        report = register_search.register(self.case, output, "Найти родителей")
        self.assertEqual(report["added_artifacts"], 1)
        self.assertEqual(report["result_status"], "partial")
        self.assertEqual(self.rows("claims-register.csv"), [])

    def test_browser_import_rejects_foreign_urls_and_conflicting_rows(self):
        page = self.directory / "browser.json"
        snapshot = {"url": "https://www.familysearch.org/en/search/record/results", "scope": {},
                    "rows": [{"name": "Иван Иванов", "url": "https://other.invalid/ark:/61903/1:1:TEST", "text": "Иван Иванов"}]}
        common.save_json(page, snapshot)
        with self.assertRaises(common.SourceError):
            fs.browser_import([page], [])
        snapshot["rows"][0]["url"] = "https://www.familysearch.org/ark:/61903/1:1:TEST"
        snapshot["rows"].append({**snapshot["rows"][0], "text": "Исправленная строка"})
        common.save_json(page, snapshot)
        with self.assertRaises(common.SourceError):
            fs.browser_import([page], [])

    def test_book_frame_index_rows_have_local_ids_and_honest_coverage(self):
        page = self.directory / "book.json"
        snapshot = {"source": "familysearch-browser-book", "book": {"group_id": "TEST-G", "expected_frames": 2},
                    "frames": [{"number": 1, "url": "https://www.familysearch.org/ark:/61903/3:1:TEST-1",
                                "status": "not-indexed", "rows": []},
                               {"number": 2, "url": "https://www.familysearch.org/ark:/61903/3:1:TEST-2",
                                "status": "indexed", "rows": [{"entry": 1, "name": "Иван Иванов",
                                "text": "Primary Иван Иванов Spouse Мария Петрова",
                                "people": [{"name": "Иван Иванов", "label": "Primary Иван Иванов"},
                                           {"name": "Мария Петрова", "label": "Spouse Мария Петрова"}]}]}]}
        common.save_json(page, snapshot)
        result = fs.browser_import([page, page], compile_patterns(surnames=["Петрова"]))
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["books"]["TEST-G"]["not_indexed_frames"], [1])
        self.assertEqual(result["coverage"]["observed_rows"], 1)
        match = result["hits"]["Петрова"][0]
        self.assertEqual(match["pid"], "observed-2")
        self.assertFalse(match["principal"])
        self.assertEqual(match["ark"], "")
        self.assertTrue(match["rid"].startswith("browser-observation:"))
        output = self.directory / "book-analysis.json"
        common.save_json(output, result)
        register_search.register(self.case, output, "Проверить индекс книги")
        artifact = self.rows("artifact-register.csv")[0]
        self.assertEqual(artifact["source_url_or_path"], snapshot["frames"][1]["url"])
        self.assertEqual(json.loads(artifact["archival_reference"])["RID"], "")
        self.assertIn("Мария Петрова", artifact["names_as_written"])
        snapshot["frames"] = snapshot["frames"][1:]
        common.save_json(page, snapshot)
        self.assertFalse(fs.browser_import([page], [])["coverage"]["complete"])

    def test_loading_and_missing_index_rows_cannot_be_complete(self):
        page = self.directory / "book.json"
        snapshot = {"source": "familysearch-browser-book", "book": {"group_id": "TEST-G", "expected_frames": 1},
                    "frames": [{"number": 1, "url": "https://www.familysearch.org/ark:/61903/3:1:TEST-1",
                                "status": "indexed", "loading": True, "rows": []}]}
        common.save_json(page, snapshot)
        with self.assertRaises(common.SourceError):
            fs.browser_import([page], [])
        snapshot["frames"][0].update(loading=False, rows=[{"entry": 2, "name": "Иван", "text": "Иван"}])
        common.save_json(page, snapshot)
        with self.assertRaises(common.SourceError):
            fs.browser_import([page], [])
class HttpAndCliTests(unittest.TestCase):
    def test_cyrillic_search_and_page_context_reach_source_unchanged(self):
        # Live GWAR returned a false empty result until the source's UTF-8 JSON
        # representation and /heroes/ page context were preserved in the port.
        client = common.SourceClient(gwar.BASE, interval=0, referer=gwar.BASE + "/heroes/")
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{"hits":{"total":0,"hits":[]}}'
        with patch.object(client.opener, "open", return_value=Response()) as opened:
            client.request("/gt_data/?builder=Heroes", {"queryFields": {"last_name": "Иванов"}})
            request = opened.call_args.args[0]
            self.assertIn("Иванов".encode("utf-8"), request.data)
            self.assertEqual(request.get_header("Referer"), gwar.BASE + "/heroes/")

    def test_html_challenge_is_access_failure_even_with_http_200(self):
        client = common.SourceClient(fs.WWW, interval=0)
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'<html><div class="smartcaptcha">human verification</div></html>'
        with patch.object(client.opener, "open", return_value=Response()) as opened:
            with self.assertRaises(common.SourceError) as caught:
                client.request("/test")
            self.assertEqual(caught.exception.status, 403)
            self.assertEqual(opened.call_count, 1)

    def test_local_filter_cli_needs_no_session_and_reports_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            common.save_json(directory / "fsk_test_frames.json", {"G|1": {"apid": "A", "rids": ["R"]}})
            common.save_json(directory / "fsk_test_records.json", {"R": parse_record(raw_record())})
            common.save_json(directory / "groups.json", {"groups": [group(("A", "B"))], "total": 1})
            env = {**os.environ, "FS_TOKEN": ""}
            result = subprocess.run([sys.executable, str(SCRIPTS / "familysearch.py"), "filter", "test", "--dir", tmp,
                                     "--groups", str(directory / "groups.json"), "--surnames", "Петрова",
                                     "--out", str(directory / "analysis.json")], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 1, result.stderr)
            output = common.read_json(directory / "analysis.json")
            self.assertEqual(len(output["candidates"]), 1)
            self.assertEqual(output["coverage"]["missing_frames"], ["G|2"])

    def test_rate_limit_and_credentials_are_not_retried_or_echoed(self):
        client = common.SourceClient(fs.WWW, token="synthetic-secret", interval=0)
        for status in (401, 403, 429):
            error = urllib.error.HTTPError(fs.WWW, status, "synthetic-secret", {}, None)
            with patch.object(client.opener, "open", side_effect=error) as opened:
                with self.assertRaises(common.SourceError) as caught:
                    client.request("/test")
                self.assertEqual(opened.call_count, 1)
                self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_cross_origin_redirect_never_forwards_session(self):
        request = urllib.request.Request(fs.WWW + "/test", headers={"Cookie": "fssessionid=synthetic-secret"})
        with self.assertRaises(common.SourceError):
            common.SameOriginRedirect().redirect_request(request, None, 302, "redirect", {}, "https://other.invalid/test")

    def test_help_is_offline_and_tree_commands_do_not_exist(self):
        for script in ("familysearch.py", "gwar.py", "register_search.py"):
            result = subprocess.run([sys.executable, str(SCRIPTS / script), "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, str(SCRIPTS / "familysearch.py"), "import", "--apply"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_outputs_cannot_be_inside_skill_and_credentials_are_not_cli_inputs(self):
        for path in (SCRIPTS / "private.json", SCRIPTS.parent):
            with self.assertRaises(common.SourceError):
                common.outside_skill(path)
        result = subprocess.run([sys.executable, str(SCRIPTS / "familysearch.py"), "harvest",
                                 "book.json", "--tag", "x", "--dir", "/tmp", "--token-file", "secret"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_state_lock_excludes_another_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            with common.state_lock(path):
                with self.assertRaises(common.SourceError):
                    with common.state_lock(path):
                        self.fail("concurrent writer")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
