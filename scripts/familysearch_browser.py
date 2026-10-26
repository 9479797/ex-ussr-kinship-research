"""Import observed browser index rows and account for selected book frames."""

from __future__ import annotations

import re
import urllib.parse

from research_common import SourceError, outside_skill, read_json, save_json, state_lock, timestamp


def fs_url(value, kind=None):
    parsed = urllib.parse.urlsplit(value or "")
    if parsed.scheme != "https" or parsed.netloc not in ("www.familysearch.org", "familysearch.org"):
        raise SourceError("Нужна HTTPS-ссылка FamilySearch без учётных данных в URL.")
    if kind and not re.fullmatch(r"/ark:/61903/" + re.escape(kind) + r":[^/]+", parsed.path):
        raise SourceError("Проверьте устойчивую ссылку записи или изображения FamilySearch.")
    return parsed


def snapshot_rows(snapshot, pages, books):
    if snapshot.get("source") != "familysearch-browser-book":
        if not isinstance(snapshot.get("rows"), list) or not isinstance(snapshot.get("scope"), dict):
            raise SourceError("Браузерный снимок должен содержать url, scope и rows.")
        fs_url(snapshot.get("url"))
        context = {"url": snapshot["url"], "scope": snapshot["scope"], "captured_at": snapshot.get("captured_at", "")}
        pages.append(context)
        return [(row, context) for row in snapshot["rows"]]
    book, frames = snapshot.get("book"), snapshot.get("frames")
    if (not isinstance(book, dict) or not book.get("group_id") or type(book.get("expected_frames")) is not int
            or book["expected_frames"] < 1 or not isinstance(frames, list)):
        raise SourceError("Для снимка книги нужны group_id, expected_frames и список frames.")
    key = str(book["group_id"])
    if key in books and books[key]["book"] != book:
        raise SourceError("Метаданные одной книги различаются между снимками.")
    state = books.setdefault(key, {"book": book, "frames": {}})
    result = []
    for frame in frames:
        if (not isinstance(frame, dict) or type(frame.get("number")) is not int
                or not 1 <= frame["number"] <= book["expected_frames"]
                or frame.get("status") not in ("indexed", "not-indexed", "failed")
                or not isinstance(frame.get("rows"), list) or frame.get("loading")):
            raise SourceError("Некорректный или незагруженный кадр браузерного индекса.")
        frame_url = fs_url(frame.get("url"), "3:1")
        url_group = urllib.parse.parse_qs(frame_url.query).get("groupId")
        if url_group and url_group != [key]:
            raise SourceError("URL кадра относится к другой группе изображений.")
        if (frame["status"] == "indexed") != bool(frame["rows"]):
            raise SourceError("Статус кадра не соответствует наличию строк индекса.")
        compare = {k: v for k, v in frame.items() if k != "captured_at"}
        old = state["frames"].get(frame["number"])
        if old and old != compare:
            raise SourceError("Содержимое одного кадра различается между снимками.")
        state["frames"][frame["number"]] = compare
        context = {"url": frame["url"], "scope": {"book": book, "frame": frame["number"], "index_status": frame["status"]},
                   "captured_at": frame.get("captured_at", "")}
        entries = set()
        for row in frame["rows"]:
            if not isinstance(row, dict) or type(row.get("entry")) is not int or row["entry"] < 1 or row["entry"] in entries:
                raise SourceError("Строки кадра должны иметь разные положительные номера entry.")
            entries.add(row["entry"])
            result.append(({**row, "url": frame["url"], "image_url": frame["url"],
                            "observed_id": f"{key}:{frame['number']}:{row['entry']}"}, context))
        if entries and entries != set(range(1, len(entries) + 1)):
            raise SourceError("В снимке кадра пропущены строки entry; не заявляйте весь индекс кадра прочитанным.")
    return result


def browser_coverage(books, pages, observed, matched):
    report = {"complete": False, "scope": "browser-observed-index-rows", "observed_rows": observed,
              "matched_rows": matched, "pages": pages}
    if not books:
        return report
    details = {}
    for key, state in books.items():
        expected = state["book"]["expected_frames"]
        frames = state["frames"]
        details[key] = {"expected_frames": expected, "observed_frames": len(frames),
                        "missing_frames": [n for n in range(1, expected + 1) if n not in frames],
                        "failed_frames": [n for n, f in frames.items() if f["status"] == "failed"],
                        "indexed_frames": [n for n, f in frames.items() if f["status"] == "indexed"],
                        "not_indexed_frames": [n for n, f in frames.items() if f["status"] == "not-indexed"]}
    report.update({"scope": "browser-observed-frame-indexes", "books": details,
                   "index_completeness": "not-established",
                   "complete": not pages and all(not value["missing_frames"] and not value["failed_frames"] for value in details.values())})
    return report


def browser_import(files, patterns):
    candidates, registry, hits, pages, books, seen = [], [], {}, [], {}, {}
    rows = []
    for path in files:
        snapshot = read_json(path)
        if not isinstance(snapshot, dict):
            raise SourceError("Некорректный браузерный снимок.")
        rows.extend(snapshot_rows(snapshot, pages, books))
    for row, context in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip() or not isinstance(row.get("text"), str):
            raise SourceError("В браузерной строке нужны исходные name, text и ссылка.")
        parsed = fs_url(row.get("url"), "3:1" if row.get("observed_id") else "1:1")
        ark = "" if row.get("observed_id") else parsed.path.split("1:1:")[-1]
        rid = "browser-observation:" + str(row["observed_id"]) if row.get("observed_id") else ark
        original = {k: row[k] for k in ("name", "text", "url", "image_url", "people", "observed_id") if k in row}
        if rid in seen:
            if seen[rid] != original:
                raise SourceError("Браузерные строки одной записи различаются; сохраните версии раздельно.")
            continue
        seen[rid] = original
        people = row.get("people") or [{"name": row["name"]}]
        if not isinstance(people, list) or any(not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"] for p in people):
            raise SourceError("Некорректный список наблюдённых имён.")
        persons = {(f"observed-{i}" if row.get("people") else "principal"): {"name": p["name"], "names": [p["name"]], "ark": ark if i == 1 else "",
                    "principal": i == 1, "gender": "", "facts": [], "indexed_label": p.get("label", "")}
                   for i, p in enumerate(people, 1)}
        matches = []
        for layer, label, regex in patterns:
            if regex.search(row["text"]):
                person_matches = [pid for pid, person in persons.items() if regex.search(person["name"])] if row.get("people") else []
                for pid in person_matches or [None]:
                    match = {"rid": rid, "pid": pid, "name": persons[pid]["name"] if pid else row["name"],
                             "principal": persons[pid]["principal"] if pid else None, "ark": ark,
                             "layer": layer, "label": label, "match_scope": "visible-row-text"}
                    hits.setdefault(label, []).append(match)
                    matches.append(match)
        registry.append({"rid": rid, "name": row["name"], "ark": ark, "indexed_text": row["text"], "url": row["url"]})
        if patterns and not matches:
            continue
        record = {"schemaVersion": 2, "persons": persons, "rels": [], "recid": "", "img": "",
                  "browser_text": row["text"], "browser_url": row["url"],
                  "identifier_kind": "local-browser-observation" if row.get("observed_id") else "record-ark",
                  "captured_at": context["captured_at"], "raw": {"browser_row": original}}
        location = {"page_url": context["url"], "image_url": row.get("image_url", ""), "scope": context["scope"]}
        candidates.append({"rid": rid, "record": record, "locations": [location], "matches": matches})
    return {"source": "familysearch", "transport": "browser", "analyzed_at": timestamp(), "registry": registry,
            "hits": hits, "candidates": candidates, "query": [{"layer": layer, "label": label, "pattern": regex.pattern}
            for layer, label, regex in patterns], "coverage": browser_coverage(books, pages, len(seen), len(candidates))}


def browser_harvest(book_file, directory, tag, batch_file=None):
    """Persist agent-observed frame batches; never authenticate or make source requests."""
    if not re.fullmatch(r"[\w-]+", tag):
        raise SourceError("Метка сбора: только буквы, цифры, подчёркивание и дефис.")
    description = read_json(book_file)
    book = description.get("book") if isinstance(description, dict) else None
    initial = {"source": "familysearch-browser-book", "book": book, "frames": []}
    snapshot_rows(initial, [], {})
    directory = outside_skill(directory)
    checkpoint = directory / f"fsk_{tag}_browser.json"
    with state_lock(directory / f"fsk_{tag}.lock"):
        saved = read_json(checkpoint) if checkpoint.exists() else initial
        if saved.get("source") != initial["source"] or saved.get("book") != book:
            raise SourceError("Метка относится к другой книге; выберите другую метку.")
        snapshot_rows(saved, [], {})
        by_number = {f["number"]: f for f in saved["frames"]}
        if batch_file:
            batch = read_json(batch_file)
            if batch.get("source") != initial["source"] or batch.get("book") != book:
                raise SourceError("Порция относится к другой книге.")
            # Validate rows as well as frame states before changing the checkpoint.
            browser_import([batch_file], [])
            for frame in batch["frames"]:
                old = by_number.get(frame["number"])
                compare = lambda item: {k: v for k, v in item.items() if k != "captured_at"}
                if old and old["status"] != "failed":
                    if compare(old) != compare(frame):
                        raise SourceError("Кадр уже сохранён с другим содержимым; сохраните новую версию отдельно.")
                    continue
                by_number[frame["number"]] = frame
        saved = {**initial, "frames": [by_number[n] for n in sorted(by_number)]}
        # Revalidate the entire checkpoint and its real coverage before persisting it.
        states = {}
        rows = snapshot_rows(saved, [], states)
        report = browser_coverage(states, [], len(rows), 0)
        save_json(checkpoint, saved)
    pending = [n for n in range(1, book["expected_frames"] + 1)
               if n not in by_number or by_number[n]["status"] == "failed"]
    return {"source": "familysearch", "transport": "browser", "tag": tag,
            "checkpoint": str(checkpoint), "coverage": report,
            "pending_frames": pending, "next_frames": pending[:5],
            "updated_at": timestamp()}
