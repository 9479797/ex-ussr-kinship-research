"""Parse indexed records and find candidates without resolving identities."""

from __future__ import annotations

import re

from research_common import SourceError


def uri_name(value):
    return str(value or "").rsplit("/", 1)[-1]


def parse_fact(fact):
    return {
        "type": uri_name(fact.get("type")),
        "formal": (fact.get("date") or {}).get("formal", ""),
        "orig": (fact.get("date") or {}).get("original", ""),
        "place": (fact.get("place") or {}).get("original", ""),
    }


def parse_record(data):
    people = data.get("persons") if isinstance(data, dict) else None
    if not isinstance(people, list) or not people or any(not isinstance(p, dict) or not p.get("id") for p in people):
        raise SourceError("Некорректная запись: ожидается непустой список persons с ID.")
    if len({p["id"] for p in people}) != len(people):
        raise SourceError("Повтор ID человека внутри записи.")
    persons = {}
    for person in people:
        names = list(dict.fromkeys(
            form["fullText"] for name in person.get("names", [])
            for form in name.get("nameForms", []) if form.get("fullText")
        ))
        persistent = person.get("identifiers", {}).get("http://gedcomx.org/Persistent", [""])
        persons[person["id"]] = {
            "name": names[0] if names else "", "names": names,
            "gender": uri_name((person.get("gender") or {}).get("type")),
            "principal": bool(person.get("principal")),
            "ark": (persistent[0] if persistent else "").split("1:1:")[-1],
            "facts": [parse_fact(fact) for fact in person.get("facts", [])],
        }
    relationships = [{
        "type": uri_name(rel.get("type")),
        "p1": (rel.get("person1") or {}).get("resourceId"),
        "p2": (rel.get("person2") or {}).get("resourceId"),
        "facts": [parse_fact(fact) for fact in rel.get("facts", [])],
    } for rel in data.get("relationships", [])]
    record_id = next((field.get("values", [{}])[0].get("text", "")
                      for field in data.get("fields", [])
                      if uri_name(field.get("type")) == "RecordId" and field.get("values")), "")
    image = next((source["about"].split("3:1:")[-1] for source in data.get("sourceDescriptions", [])
                  if "3:1:" in source.get("about", "")), "")
    return {"schemaVersion": 2, "persons": persons, "rels": relationships, "recid": record_id, "img": image}


def normalize_record(record):
    if isinstance(record, dict) and record.get("raw") and record.get("schemaVersion") != 2:
        return {**parse_record(record["raw"]), "raw": record["raw"]}
    return record


def valid_record(record):
    return (isinstance(record, dict) and not record.get("err")
            and isinstance(record.get("persons"), dict) and bool(record["persons"]))


def coverage(frames, records, expected=None):
    expected = list(frames) if expected is None else list(expected)
    report = {"expected_frames": len(expected), "successful_frames": 0,
              "empty_frames": {}, "failed_frames": {}, "missing_frames": [],
              "failed_records": {}, "missing_records": []}
    record_ids = set()
    for key in expected:
        frame = frames.get(key)
        if frame is None:
            report["missing_frames"].append(key)
        elif frame.get("err") or not isinstance(frame.get("rids"), list):
            report["failed_frames"][key] = frame.get("err") or "Некорректный кадр"
        else:
            report["successful_frames"] += 1
            record_ids.update(frame["rids"])
            if not frame["rids"]:
                group_id, number = key.rsplit("|", 1)
                report["empty_frames"].setdefault(group_id, []).append(int(number))
    for rid in sorted(record_ids):
        if rid not in records:
            report["missing_records"].append(rid)
        elif not valid_record(records[rid]):
            report["failed_records"][rid] = records[rid].get("err") or "Некорректная запись"
    report["expected_records"] = len(record_ids)
    report["successful_records"] = len(record_ids) - len(report["missing_records"]) - len(report["failed_records"])
    report["complete"] = not any(report[key] for key in ("missing_frames", "failed_frames", "missing_records", "failed_records"))
    report["scope"] = "retrieval-of-supplied-frames-only"
    return report


def compile_patterns(config=None, surnames=(), beacons=()):
    config = config or {}
    if not isinstance(config, dict) or set(config) - {"_comment", "surnames", "beacons", "inferMarriageFromBirthYear"}:
        raise SourceError("Неизвестные поля конфигурации поиска.")
    if config.get("inferMarriageFromBirthYear", False) is not False:
        raise SourceError("Предположение о браке по году рождения не поддерживается; читайте оригинал.")
    definitions = []
    for layer, extra in (("surnames", surnames), ("beacons", beacons)):
        values = config.get(layer, {})
        if not isinstance(values, dict):
            raise SourceError(f"{layer}: ожидается объект метка → выражение.")
        definitions.extend((layer, label, pattern) for label, pattern in values.items())
        definitions.extend((layer, pattern, pattern) for pattern in extra)
    result, labels = [], set()
    for layer, label, pattern in definitions:
        if not isinstance(label, str) or not label.strip() or label in labels or not isinstance(pattern, str) or not pattern:
            raise SourceError("Пустая/повторная метка или некорректное выражение.")
        labels.add(label)
        try:
            result.append((layer, label, re.compile(pattern, re.IGNORECASE)))
        except re.error as exc:
            raise SourceError(f"Некорректное Python regex для метки {label}.") from exc
    return result


def classify(record, person_id):
    facts = record["persons"][person_id].get("facts", [])
    couple_facts = [fact for rel in record.get("rels", []) if rel["type"] == "Couple"
                    and person_id in (rel["p1"], rel["p2"]) for fact in rel.get("facts", [])]
    for kind, label in (("Death", "Смерть"), ("Marriage", "Брак"), ("Birth", "Рождение")):
        fact = next((item for item in facts + (couple_facts if kind == "Marriage" else []) if item["type"] == kind), None)
        if fact:
            return {"ev": label, "date": fact.get("orig") or fact.get("formal", ""),
                    "place": fact.get("place", ""), "inferred": False}
    return {"ev": "?", "date": "", "place": "", "inferred": False}


def analyze(frames, records, metadata, expected, patterns):
    report = coverage(frames, records, expected)
    registry, hits, candidates = [], {}, []
    locations = {}
    for key in sorted(frames, key=lambda value: (value.rsplit("|", 1)[0], int(value.rsplit("|", 1)[1]))):
        frame = frames[key]
        if frame.get("err"):
            continue
        group_id, number = key.rsplit("|", 1)
        for rid in frame.get("rids", []):
            if not valid_record(records.get(rid)):
                continue
            location = {"gid": group_id, "frame": int(number), "apid": frame["apid"], **metadata.get(group_id, {})}
            locations.setdefault(rid, []).append(location)
            record = records[rid]
            for pid, person in record["persons"].items():
                if not person.get("principal"):
                    continue
                parents = [record["persons"].get(rel["p1"], {}) for rel in record.get("rels", [])
                           if rel["type"] == "ParentChild" and rel["p2"] == pid]
                spouses = [record["persons"].get(rel["p2"] if rel["p1"] == pid else rel["p1"], {}).get("name", "")
                           for rel in record.get("rels", []) if rel["type"] == "Couple" and pid in (rel["p1"], rel["p2"])]
                registry.append({**location, "rid": rid, "pid": pid, "name": person.get("name", ""),
                                 "ark": person.get("ark", ""), **classify(record, pid),
                                 "parents": [parent.get("name", "") for parent in parents],
                                 "father": next((p.get("name", "") for p in parents if p.get("gender") == "Male"), ""),
                                 "mother": next((p.get("name", "") for p in parents if p.get("gender") == "Female"), ""),
                                 "spouses": spouses, "recid": record.get("recid", ""), "img": record.get("img", "")})
    for rid, source_locations in locations.items():
        record = records[rid]
        matching = []
        for pid, person in record["persons"].items():
            names = person.get("names") or [person.get("name", "")]
            for layer, label, regex in patterns:
                if any(regex.search(name) for name in names):
                    hit = {"rid": rid, "pid": pid, "name": person.get("name", ""),
                           "principal": bool(person.get("principal")), "ark": person.get("ark", ""), "layer": layer}
                    hits.setdefault(label, []).append(hit)
                    matching.append({**hit, "label": label})
        if matching:
            # One candidate artifact per indexed record; all matching indexed people stay distinct.
            candidates.append({"rid": rid, "record": record, "locations": source_locations, "matches": matching})
    return {"source": "familysearch", "registry": registry, "hits": hits,
            "candidates": candidates, "gmeta": metadata, "coverage": report}
