#!/usr/bin/env python3
"""Register source-search leads in an existing case without asserting identities."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import urllib.parse
from pathlib import Path

from familysearch_records import classify
from research_common import (SourceError, atomic_bytes, fingerprint, outside_skill,
                             output_json, read_json, state_lock, timestamp)


def read_register(path, id_field):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        if not fields or id_field not in fields:
            raise SourceError(f"Некорректный реестр {path.name}.")
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise SourceError(f"Повреждена строка CSV: {path.name}.")
    ids = [row[id_field] for row in rows]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise SourceError(f"Пустые или повторные ID в {path.name}.")
    return fields, rows


def save_register(path, fields, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_bytes(path, stream.getvalue().encode("utf-8"))


def next_id(rows, field, prefix):
    numbers = [int(match.group(1)) for row in rows if (match := re.fullmatch(re.escape(prefix) + r"(\d+)", row[field]))]
    return f"{prefix}{max(numbers, default=0) + 1:04d}"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def fs_items(data):
    if "candidates" in data:
        candidates = data["candidates"]
    elif isinstance(data.get("records"), list):
        candidates = [{"rid": record.get("rid"), "record": record,
                       "locations": [{"apid": data["apid"]}] if data.get("apid") else []}
                      for record in data["records"]]
    else:
        raise SourceError("Для FamilySearch передайте результат filter или search.")
    for candidate in candidates:
        record, rid = candidate.get("record"), candidate.get("rid")
        if not rid or not isinstance(record, dict) or not isinstance(record.get("persons"), dict) or not record["persons"]:
            raise SourceError("Некорректный кандидат FamilySearch.")
        people = record["persons"]
        principal = next((pid for pid, person in people.items() if person.get("principal")), next(iter(people)))
        ark = people[principal].get("ark", "")
        if record.get("browser_url"):
            url = record["browser_url"]
        elif ark.startswith("https://www.familysearch.org/"):
            url = ark
        elif ark and not urllib.parse.urlsplit(ark).scheme:
            url = "https://www.familysearch.org/ark:/61903/1:1:" + urllib.parse.quote(ark, safe="")
        else:
            url = "https://www.familysearch.org/platform/records/records/" + urllib.parse.quote(str(rid), safe="")
        event = classify(record, principal)
        locations = candidate.get("locations", [])
        reference = {"RID": rid if record.get("identifier_kind") != "local-browser-observation" else "",
                     "local_observation_id": rid if record.get("identifier_kind") == "local-browser-observation" else "",
                     "RecordId": record.get("recid", ""), "image": record.get("img", ""), "locations": locations}
        names = list(dict.fromkeys(name for person in people.values() for name in (person.get("names") or [person.get("name", "")]) if name))
        yield {"id": str(rid), "snapshot": record, "locations": locations,
               "captured_at": record.get("captured_at", ""), "source_title": "FamilySearch — индекс записи " + str(rid),
               "source_url_or_path": url, "repository_or_archive": "FamilySearch; архив оригинала уточнить",
               "archival_reference": encode(reference), "record_date": event["date"], "place_as_written": event["place"],
               "names_as_written": "; ".join(names),
               "summary": ("Кандидат из видимой текстовой строки индекса; поля и родство не интерпретированы."
                           if record.get("browser_text") else "Кандидат из индекса; " + event["ev"] + ". Родство и личность не проверены.")}


def gwar_items(data):
    if not isinstance(data.get("hits"), list):
        raise SourceError("Для GWAR передайте результат search или volost.")
    for hit in data["hits"]:
        if not isinstance(hit, dict) or not hit.get("_id") or not isinstance(hit.get("_source"), dict):
            raise SourceError("Некорректная карточка GWAR.")
        source = hit["_source"]
        identity = "|".join(str(hit.get(field, "")) for field in ("_index", "_type", "_id"))
        name = " ".join(str(source.get(field) or "") for field in ("last_name", "first_name", "middle_name")).strip()
        yield {"id": identity, "snapshot": hit, "captured_at": data.get("captured_at", ""),
               "source_title": "GWAR — " + (name or str(hit["_id"])),
               "source_url_or_path": "https://gwar.mil.ru/heroes/", "repository_or_archive": "GWAR; архив оригинала уточнить",
               "archival_reference": encode({"card": identity, "documents_pages": source.get("documents_pages", {}),
                                              "military_unit_name": source.get("military_unit_name", "")}),
               "record_date": str(source.get("event_date_from") or ""),
               "place_as_written": str(source.get("birth_place") or ""), "names_as_written": name,
               "summary": "Карточка Первой мировой; место и одноимённые лица требуют проверки по документу."}


def without_capture(value):
    if isinstance(value, dict):
        return {key: without_capture(item) for key, item in value.items() if key != "captured_at"}
    if isinstance(value, list):
        return [without_capture(item) for item in value]
    return value


def snapshot_key(source, identity, snapshot):
    snapshot = without_capture(snapshot)
    if source == "gwar" and isinstance(snapshot, dict):
        # Search ranking varies between name and locality queries of the same card.
        snapshot = {key: value for key, value in snapshot.items() if key not in ("_score", "sort", "_explanation")}
    return fingerprint({"source": source, "id": identity, "snapshot": snapshot})


def existing_snapshot_keys(case, artifacts):
    """Reuse earlier snapshots without rewriting manual assessments or old keys."""
    keys = {}
    for row in artifacts:
        notes = row.get("access_notes", "")
        if "snapshot=" not in notes:
            continue
        relative = Path(notes.split("snapshot=", 1)[1])
        if relative.parts[:2] != ("sources", "gwar"):
            continue
        path = (case / relative).resolve()
        if case not in path.parents or not path.is_file():
            continue
        try:
            saved = read_json(path)
            if saved.get("source") == "gwar" and saved.get("source_record_id") and isinstance(saved.get("record"), dict):
                keys.setdefault(snapshot_key("gwar", saved["source_record_id"], saved["record"]), row)
        except (OSError, ValueError, AttributeError):
            continue
    return keys


def register(case, input_path, goal):
    case = outside_skill(case)
    input_bytes = Path(input_path).expanduser().read_bytes()
    data = json.loads(input_bytes.decode("utf-8-sig"))
    if not isinstance(data, dict) or data.get("source") not in ("familysearch", "gwar"):
        raise SourceError("Нужен JSON поискового результата FamilySearch или GWAR.")
    source = data["source"]
    # Validate the complete input before changing any register.
    items = list(fs_items(data) if source == "familysearch" else gwar_items(data))
    coverage = data.get("coverage")
    if not isinstance(coverage, dict) or type(coverage.get("complete")) is not bool:
        raise SourceError("В результате отсутствует отчёт о покрытии.")
    for name in ("person-register.csv", "claims-register.csv", "relationship-register.csv", "case-intake.yaml"):
        if not (case / name).is_file():
            raise SourceError("Сначала создайте дело через init_case.py.")
    with state_lock(case / ".register-search.lock"):
        artifact_path, log_path = case / "artifact-register.csv", case / "search-log.csv"
        artifact_fields, artifacts = read_register(artifact_path, "artifact_id")
        log_fields, logs = read_register(log_path, "search_id")
        required_artifact = {"artifact_id", "access_notes", "originality", "directness", "status", "sha256", "independence_group"}
        required_log = {"search_id", "filters", "artifact_ids", "result_status"}
        if not required_artifact.issubset(artifact_fields) or not required_log.issubset(log_fields):
            raise SourceError("Формат реестров не соответствует шаблонам навыка.")
        existing_keys = existing_snapshot_keys(case, artifacts) if source == "gwar" else {}
        artifact_ids, added = [], 0
        for item in items:
            key = snapshot_key(source, item["id"], item["snapshot"])
            marker = "source_key=" + key
            previous = existing_keys.get(key) or next((row for row in artifacts if marker in row.get("access_notes", "")), None)
            if previous:
                artifact_ids.append(previous["artifact_id"])
                continue
            # Save the actual source payload, not just a surname-match excerpt.
            payload = {"source": source, "source_record_id": item["id"], "record": item["snapshot"],
                       "locations": item.get("locations", []), "registered_at": timestamp()}
            snapshot = case / "sources" / source / (key + ".json")
            # Reject symlinks that would put private case material inside the skill.
            outside_skill(snapshot)
            content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if snapshot.exists():
                content = snapshot.read_bytes()
                existing = json.loads(content)
                if (existing.get("source_record_id") != item["id"]
                        or without_capture(existing.get("record")) != without_capture(item["snapshot"])):
                    raise SourceError("Сохранённый артефакт не соответствует исходной записи.")
            else:
                atomic_bytes(snapshot, content)
            artifact_id = next_id(artifacts, "artifact_id", "A-")
            row = {field: "" for field in artifact_fields}
            row.update({key: value for key, value in item.items() if key in row})
            row.update({"artifact_id": artifact_id, "captured_at": item["captured_at"] or timestamp(),
                        "source_type": "index", "originality": "index", "directness": "lead-only", "status": "lead-only",
                        "provenance_level": "источник индекса установлен; оригинал не проверен",
                        "access_notes": f"{marker}; snapshot={snapshot.relative_to(case)}",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "reliability_notes": "Совпадение — кандидат. Независимость не установлена; назначьте группу исходного документа после проверки."
                        + (" Дата исходного получения неизвестна; captured_at — дата регистрации." if not item["captured_at"] else "")})
            artifacts.append(row)
            existing_keys[key] = row
            artifact_ids.append(artifact_id)
            added += 1
        artifact_ids = list(dict.fromkeys(artifact_ids))
        complete = coverage["complete"] and not data.get("blocked")
        status = "found" if artifact_ids else "not-found"
        if not complete:
            status = "blocked" if data.get("blocked") else "partial"
        log_key = fingerprint({"source": source, "query": data.get("query"), "coverage": coverage,
                               "artifacts": artifact_ids, "goal": goal, "input": fingerprint(data)})
        log_marker = "search_key=" + log_key
        new_log = not any(log_marker in row.get("filters", "") for row in logs)
        if new_log:
            row = {field: "" for field in log_fields}
            row.update({"search_id": next_id(logs, "search_id", "S-"), "timestamp": timestamp(), "research_goal": goal,
                        "query": encode(data.get("query", {"apid": data.get("apid")})),
                        "source_or_domain": "familysearch.org" if source == "familysearch" else "gwar.mil.ru",
                        "filters": encode({"key": log_marker, "coverage": coverage, "entities": data.get("entities"),
                                           "input": str(Path(input_path).expanduser().resolve()), "sha256": hashlib.sha256(input_bytes).hexdigest()}),
                        "result_status": status, "artifact_ids": ";".join(artifact_ids),
                        "blocker": encode(data["blocked"]) if data.get("blocked") else "",
                        "next_step": "Проверить оригиналы, различить кандидатов и установить независимость; покрытие ограничено указанным набором."})
            logs.append(row)
        # A rerun after an interruption between these writes reuses source_key and search_key.
        save_register(artifact_path, artifact_fields, artifacts)
        save_register(log_path, log_fields, logs)
        return {"added_artifacts": added, "artifact_ids": artifact_ids, "added_search_log": new_log,
                "result_status": status, "claims_created": 0, "persons_created": 0, "relationships_created": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Записать поисковые находки в существующие реестры как непроверенные артефакты.")
    parser.add_argument("--case-dir", required=True)
    parser.add_argument("--input", required=True, help="Результат FamilySearch filter/search или GWAR search/volost")
    parser.add_argument("--research-goal", required=True)
    args = parser.parse_args(argv)
    try:
        output_json(register(args.case_dir, args.input, args.research_goal))
        return 0
    except (SourceError, OSError, ValueError, TypeError, KeyError) as exc:
        print("Ошибка: " + (str(exc) if isinstance(exc, SourceError) else "Некорректный файл или ошибка локального ввода/вывода."), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
