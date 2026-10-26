#!/usr/bin/env python3
"""Checkpoint browser-led FamilySearch research and search saved indexes offline."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from familysearch_browser import browser_import, browser_harvest
from familysearch_records import analyze, compile_patterns, normalize_record
from research_common import SourceError, fingerprint, outside_skill, output_json, read_json, timestamp

WWW = "https://www.familysearch.org"


def validate_groups(data, allow_empty=False):
    groups = data.get("groups") if isinstance(data, dict) else None
    if (not isinstance(groups, list) or (not groups and not allow_empty) or data.get("complete") is False
            or data.get("total", len(groups)) != len(groups)):
        raise SourceError("Нужен проверенный полный groups.json или явно выбранный поднабор с исправленным total.")
    seen = set()
    for group in groups:
        if (not isinstance(group, dict) or not isinstance(group.get("id"), str) or not group["id"]
                or "|" in group["id"] or group["id"] in seen or not isinstance(group.get("apids"), list)
                or any(not isinstance(apid, str) or not apid for apid in group["apids"])):
            raise SourceError("Некорректный или повторный ID книги; проверьте список кадров.")
        seen.add(group["id"])
    return groups


def merge_entries(destination, source, label):
    if not isinstance(source, dict):
        raise SourceError(f"Некорректный файл {label}.")
    for key, value in source.items():
        previous = destination.get(key)
        compare = lambda item: {k: v for k, v in item.items() if k not in ("raw", "captured_at")}
        if previous and not previous.get("err") and not value.get("err") and compare(previous) != compare(value):
            raise SourceError(f"Разные сохранённые значения {label} {key}; сначала разрешите конфликт.")
        if previous is None or previous.get("err") or (not value.get("err") and (not previous.get("raw") or value.get("raw"))):
            destination[key] = value


def filter_harvests(tags, directory, group_files, patterns):
    frames, records, metadata, expected = {}, {}, {}, {}
    inputs = []
    for tag in tags:
        if not re.fullmatch(r"[\w-]+", tag):
            raise SourceError("Некорректная метка загрузки.")
        for kind, destination in (("frames", frames), ("records", records)):
            path = Path(directory) / f"fsk_{tag}_{kind}.json"
            data = read_json(path)
            if kind == "records":
                data = {rid: normalize_record(record) for rid, record in data.items()}
            merge_entries(destination, data, kind)
            inputs.append({"path": str(path.resolve()), "sha256": fingerprint(data)})
    for path in group_files:
        for group in validate_groups(read_json(path), allow_empty=True):
            info = {key: group.get(key) for key in ("title", "dgs", "years", "places", "frames", "indexed")}
            if group["id"] in metadata and metadata[group["id"]] != info:
                raise SourceError("Конфликт метаданных книги.")
            metadata[group["id"]] = info
            for index, apid in enumerate(group["apids"], 1):
                key = f"{group['id']}|{index}"
                if key in expected and expected[key] != apid:
                    raise SourceError("Конфликт порядка кадров.")
                expected[key] = apid
    for key, frame in frames.items():
        if key not in expected or frame.get("apid") != expected[key]:
            raise SourceError("Кадры не соответствуют --groups; передайте списки всех объединяемых книг.")
    result = analyze(frames, records, metadata, expected, patterns)
    result.update({"analyzed_at": timestamp(), "inputs": inputs,
                   "query": [{"layer": layer, "label": label, "pattern": regex.pattern} for layer, label, regex in patterns]})
    return result


def parser():
    root = argparse.ArgumentParser(description="FamilySearch: сбор через текущую браузерную сессию, локальные контрольные точки и поиск. Без отдельной авторизации.")
    commands = root.add_subparsers(dest="command", required=True)
    harvest = commands.add_parser("harvest", help="Планировать и сохранять сбор индекса через встроенный браузер; сетевые действия выполняет агент")
    harvest.add_argument("book", help="JSON с наблюдёнными в браузере метаданными book")
    harvest.add_argument("--tag", required=True)
    harvest.add_argument("--dir", required=True)
    harvest.add_argument("--batch", help="Снимок прочитанной браузером порции кадров")
    filtering = commands.add_parser("filter", help="Локальный поиск в ранее сохранённых кэшах Node.js/Python")
    filtering.add_argument("tags", nargs="+")
    filtering.add_argument("--dir", required=True)
    filtering.add_argument("--groups", nargs="+", required=True)
    browser = commands.add_parser("browser-import", help="Искать во всём индексе, собранном через браузер")
    browser.add_argument("files", nargs="+")
    for command in (filtering, browser):
        command.add_argument("--surnames", nargs="+", default=[])
        command.add_argument("--beacons", nargs="+", default=[])
        command.add_argument("--config")
    for command in (harvest, filtering, browser):
        command.add_argument("--out", help="JSON вне папки навыка; по умолчанию stdout")
    return root


def run(args):
    if args.out:
        outside_skill(args.out)
    if args.command == "harvest":
        result = browser_harvest(args.book, args.dir, args.tag, args.batch)
        output_json(result, args.out)
        # A successful planning/checkpoint operation can still have pending frames.
        # The agent continues until complete or a concrete browser access block.
        return 0
    config = read_json(args.config) if args.config else {}
    patterns = compile_patterns(config, args.surnames, args.beacons)
    if args.command == "browser-import":
        result = browser_import(args.files, patterns)
    else:
        result = filter_harvests(args.tags, Path(args.dir).expanduser(), args.groups, patterns)
    output_json(result, args.out)
    return 0 if args.command == "browser-import" or result["coverage"]["complete"] else 1


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (SourceError, OSError, ValueError, TypeError, KeyError) as exc:
        message = str(exc) if isinstance(exc, SourceError) else "Некорректный входной файл или ошибка локального чтения/записи."
        print("Ошибка: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
