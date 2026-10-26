#!/usr/bin/env python3
"""Optional authenticated index retrieval; independent of the browser workflow."""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
from pathlib import Path

from familysearch import validate_groups
from familysearch_records import (coverage, frame_list_coverage, include_book_coverage,
                                  normalize_record, parse_record, valid_record)
from research_common import (SourceClient, SourceError, fingerprint, outside_skill,
                             output_json, read_json, save_json, state_lock, timestamp)

WWW = "https://www.familysearch.org"
SEARCH_QUERY = """query S($si: SearchInput!) { search(searchInput: $si) { totalCount groups {
 id title imageGroupNumber childCount indexedChildCount imageApids
 yearRange { fromYear toYear } coverages { place { fullName id } } } } }"""
FIELDS_QUERY = '{ __type(name: "SearchInput") { inputFields { name } } }'
ACCESS_STOPS = {301, 302, 303, 307, 308, 401, 403, 429}


def load_token(path=None):
    token = outside_skill(path).read_text(encoding="utf-8").strip() if path else os.environ.get("FS_TOKEN", "").strip()
    if token.startswith("fssessionid="):
        token = token.split("=", 1)[1]
    if not token or re.search(r"[\s;]", token):
        raise SourceError("Для необязательного API-маршрута нужна предоставленная сессия: FS_TOKEN или --token-file. Не вставляйте её в чат. Браузер работает отдельно.")
    return token


def check_session(client):
    data = client.request("/platform/users/current.json")
    if not isinstance(data, dict) or not isinstance(data.get("users"), list) or not data["users"]:
        raise SourceError("FamilySearch не подтвердил действующую сессию.", 401)
    # No profile fields or account identifiers are returned or saved.
    return {"source": "familysearch", "transport": "api", "session_valid": True, "checked_at": timestamp()}


def quoted(value):
    return urllib.parse.quote(str(value), safe="")


def graphql(client, query, variables=None):
    response = client.request("/records/images/orchestration/", {"query": query, "variables": variables or {}})
    if not isinstance(response, dict) or response.get("errors") or not isinstance(response.get("data"), dict):
        raise SourceError("FamilySearch GraphQL: формат изменился или запрос отклонён.")
    return response["data"]


def book_page(client, query):
    page = graphql(client, SEARCH_QUERY, {"si": query}).get("search")
    if (not isinstance(page, dict) or type(page.get("totalCount")) is not int
            or page["totalCount"] < 0 or not isinstance(page.get("groups"), list)):
        raise SourceError("Некорректная страница списка книг.")
    for group in page["groups"]:
        if (not isinstance(group, dict) or not isinstance(group.get("id"), str) or not group["id"]
                or "|" in group["id"] or not isinstance(group.get("imageApids"), list)
                or any(not isinstance(apid, str) or not apid for apid in group["imageApids"])):
            raise SourceError("В группе книг отсутствуют ID или список imageApids.")
    return page


def discover_books(client, query):
    first = book_page(client, query)
    total, groups = first["totalCount"], {}

    def append(page):
        if page["totalCount"] != total:
            raise SourceError("Состав выдачи изменился; повторите поиск книг.")
        previous = len(groups)
        for group in page["groups"]:
            if group["id"] in groups and groups[group["id"]] != group:
                raise SourceError("Метаданные книги изменились во время поиска.")
            groups[group["id"]] = group
        return len(groups) > previous

    append(first)
    if len(groups) < total:
        try:
            fields = graphql(client, FIELDS_QUERY).get("__type") or {}
            fields = {field["name"] for field in fields.get("inputFields", [])}
        except SourceError as exc:
            if exc.status is not None:
                raise
            fields = set()
        field = "offset" if "offset" in fields else "page" if "page" in fields else None
        if field:
            position = len(first["groups"]) if field == "offset" else 2
            while len(groups) < total:
                page = book_page(client, {**query, field: position})
                if not append(page):
                    break
                position += len(page["groups"]) if field == "offset" else 1
        else:
            append(book_page(client, {**query, "pageSize": total}))
    if len(groups) != total:
        raise SourceError(f"Получено {len(groups)} из {total} групп; сузьте место или набор DGS. Полный список не сохранён.")
    normalized = []
    for group in groups.values():
        years = group.get("yearRange") or {}
        places = {(item.get("place") or {}).get("fullName", "?") for item in group.get("coverages", [])}
        normalized.append({"id": group["id"], "title": group.get("title"),
                           "dgs": group.get("imageGroupNumber"), "frames": group.get("childCount"),
                           "indexed": group.get("indexedChildCount"),
                           "years": [years.get("fromYear"), years.get("toYear")],
                           "places": sorted(places), "apids": group["imageApids"]})
    frame_lists = frame_list_coverage(normalized)
    return {"source": "familysearch", "transport": "api", "total": total, "complete": True,
            "frame_list_coverage": frame_lists,
            "frame_lists_complete": all(book["complete"] for book in frame_lists.values()),
            "query": query, "captured_at": timestamp(), "groups": normalized}


class FrameReader:
    def __init__(self, client, session_client):
        self.client = client
        self.session_client = session_client

    def read(self, apid):
        data = self.client.request(f"/platform/records/images/{quoted(apid)}/records")
        if data == {}:
            # A previous successful check says nothing about the session at this frame.
            checked = check_session(self.session_client)
            return {"apid": apid, "rids": [], "err": None, "empty_response": True,
                    "session_checked_at": checked["checked_at"], "captured_at": timestamp()}
        descriptions = data.get("sourceDescriptions") if isinstance(data, dict) else None
        if not isinstance(descriptions, list) or any(not isinstance(d, dict) or not isinstance(d.get("id"), str) or not d["id"] for d in descriptions):
            raise SourceError("Некорректный ответ кадра; не учитывать его как пустой.")
        return {"apid": apid, "rids": list(dict.fromkeys(d["id"] for d in descriptions)),
                "err": None, "captured_at": timestamp()}


def fetch_record(client, rid, retain_raw=True):
    data = client.request(f"/platform/records/records/{quoted(rid)}?flag=fsh")
    record = {**parse_record(data), "captured_at": timestamp()}
    if retain_raw:
        record["raw"] = data
    return record


def harvest(groups, directory, tag, client, frame_reader, phase="ab", retain_raw=True, refresh_empty=False):
    if phase not in ("a", "b", "ab") or not re.fullmatch(r"[\w-]+", tag):
        raise SourceError("Проверьте фазу и метку загрузки.")
    groups = validate_groups({"groups": groups})
    directory = outside_skill(directory)
    prefix = directory / ("fsk_" + tag)
    paths = {kind: Path(str(prefix) + "_" + kind + ".json") for kind in ("frames", "records", "manifest")}
    identity = sorted(({"id": g["id"], "apids": g["apids"]} for g in groups), key=lambda g: g["id"])
    expected = {f"{group['id']}|{number}": apid for group in groups for number, apid in enumerate(group["apids"], 1)}
    if not expected:
        raise SourceError("В выбранных группах нет доступных кадров.")
    with state_lock(str(prefix) + ".lock"):
        manifest = read_json(paths["manifest"]) if paths["manifest"].exists() else None
        if manifest and manifest.get("groupsHash") != fingerprint(identity):
            raise SourceError("Метка относится к другому составу кадров; используйте другую метку.")
        frames = read_json(paths["frames"]) if paths["frames"].exists() else {}
        records = read_json(paths["records"]) if paths["records"].exists() else {}
        if not isinstance(frames, dict) or not isinstance(records, dict):
            raise SourceError("Некорректные файлы состояния загрузки.")
        for key, frame in frames.items():
            if not isinstance(frame, dict) or expected.get(key) != frame.get("apid"):
                raise SourceError("Сохранённые кадры не соответствуют выбранным книгам.")
        records = {rid: normalize_record(record) for rid, record in records.items()}
        save_json(paths["manifest"], {"version": 1, "groupsHash": fingerprint(identity),
                                    "expectedFrames": len(expected), "groups": identity,
                                    "frame_list_coverage": frame_list_coverage(groups),
                                    "source": "familysearch", "transport": "api", "updated_at": timestamp()})

        def checkpoint():
            save_json(paths["frames"], frames)
            save_json(paths["records"], records)

        blocked = None
        try:
            if "a" in phase:
                completed = 0
                for key, apid in expected.items():
                    previous = frames.get(key)
                    ambiguous_cache = previous and previous.get("empty_response") and not previous.get("session_checked_at")
                    if previous and not previous.get("err") and isinstance(previous.get("rids"), list) and not ambiguous_cache and not (refresh_empty and not previous["rids"]):
                        continue
                    try:
                        frames[key] = frame_reader.read(apid)
                    except SourceError as exc:
                        frames[key] = {"apid": apid, "rids": [], "err": client.redact(exc)}
                        if exc.status in ACCESS_STOPS:
                            raise
                    completed += 1
                    if completed % 50 == 0:
                        checkpoint()
                checkpoint()
            if "b" in phase:
                rids = sorted({rid for frame in frames.values() if not frame.get("err") for rid in frame.get("rids", [])})
                completed = 0
                for rid in rids:
                    old = records.get(rid)
                    if valid_record(old) and (not retain_raw or "raw" in old):
                        continue
                    try:
                        records[rid] = fetch_record(client, rid, retain_raw)
                    except SourceError as exc:
                        records[rid] = {"err": client.redact(exc)}
                        if exc.status in ACCESS_STOPS:
                            raise
                    completed += 1
                    if completed % 100 == 0:
                        checkpoint()
        except SourceError as exc:
            blocked = {"status": exc.status, "message": client.redact(exc)}
        finally:
            checkpoint()
        report = include_book_coverage(coverage(frames, records, expected), groups)
        report.update({"source": "familysearch", "transport": "api", "tag": tag, "phase": phase,
                       "phase_complete": (report["frame_lists_complete"] and not report["failed_frames"] and not report["missing_frames"]) if phase == "a" else report["complete"],
                       "raw_retained": retain_raw, "captured_at": timestamp(), "blocked": blocked})
        return report


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Нужно положительное число.")
    return number


def parser():
    root = argparse.ArgumentParser(allow_abbrev=False, description="Необязательный скриптовый поиск FamilySearch: places → books → harvest. Требует отдельно предоставленную сессию; браузерный маршрут не меняется.")
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-session", allow_abbrev=False, help="Проверить сессию без вывода токена или данных профиля")
    places = commands.add_parser("places", allow_abbrev=False, help="Найти варианты местности")
    places.add_argument("name")
    places.add_argument("--page", type=positive, default=1)
    books = commands.add_parser("books", allow_abbrev=False, help="Найти книги и ID кадров")
    choice = books.add_mutually_exclusive_group(required=True)
    choice.add_argument("--place", type=positive)
    choice.add_argument("--dgs", nargs="+")
    books.add_argument("--page-size", type=positive, default=100)
    download = commands.add_parser("harvest", allow_abbrev=False, help="Получить ID по кадрам и полные индексированные записи с возобновлением")
    download.add_argument("groups")
    download.add_argument("--tag", required=True)
    download.add_argument("--dir", required=True)
    download.add_argument("--phase", choices=("a", "b", "ab"), default="ab")
    raw = download.add_mutually_exclusive_group()
    raw.add_argument("--raw", dest="raw", action="store_true", help="Сохранять полный исходный JSON (по умолчанию)")
    raw.add_argument("--no-raw", dest="raw", action="store_false", help="Только нормализованные поля; не полная копия ответа")
    download.set_defaults(raw=True)
    download.add_argument("--refresh-empty", action="store_true")
    search = commands.add_parser("search", allow_abbrev=False, help="Проверить один кадр или ограниченную выдачу по имени")
    target = search.add_mutually_exclusive_group()
    target.add_argument("--apid")
    target.add_argument("--surname")
    search.add_argument("--given")
    search.add_argument("--place")
    search.add_argument("--pages", type=positive, default=2)
    for command in (check, places, books, download, search):
        command.add_argument("--token-file", help="Приватный файл: Value cookie fssessionid (или fssessionid=VALUE); иначе FS_TOKEN. Inspect → Application → Cookies; пользователь копирует сам.")
        command.add_argument("--out", help="JSON вне папки навыка; по умолчанию stdout")
    return root


def run(args):
    if args.out:
        outside_skill(args.out)
    groups = None
    if args.command == "harvest":
        groups = validate_groups(read_json(args.groups))
        outside_skill(args.dir)
    if args.command == "books" and args.dgs and any(not re.fullmatch(r"\d+", film) for film in args.dgs):
        raise SourceError("DGS должен состоять из цифр; сохраняйте ведущие нули.")
    if args.command == "search" and (not (args.apid or args.surname or args.given) or args.apid and (args.given or args.place)):
        raise SourceError("Выберите --apid либо --surname/--given с необязательным --place.")
    token = load_token(args.token_file)
    client = SourceClient(WWW, token=token)
    session = SourceClient("https://api.familysearch.org", token=token)
    if args.command == "check-session":
        result = check_session(session)
    elif args.command == "places":
        query = urllib.parse.urlencode({"text": args.name, "pagenum": args.page, "pagesize": 10,
                                        "output": "summary", "acceptLanguage": "en", "partial": "true", "hideUnsearchable": "true"})
        data = client.request("/service/standards/place/ws-ui/places/request?" + query)
        if not isinstance(data, dict):
            raise SourceError("Некорректный ответ поиска мест.")
        result = {"source": "familysearch", "transport": "api", "query": args.name,
                  "page": args.page, "captured_at": timestamp(), "response": data}
    elif args.command == "books":
        query = {"pageSize": args.page_size}
        query.update({"place": args.place} if args.place else {"imageGroupNumbers": args.dgs})
        result = discover_books(client, query)
    elif args.command == "harvest":
        result = harvest(groups, args.dir, args.tag, client, FrameReader(client, session), args.phase, args.raw, args.refresh_empty)
        output_json(result, args.out)
        return 0 if result["phase_complete"] and not result["blocked"] else 1
    elif args.apid:
        frame = FrameReader(client, session).read(args.apid)
        result = {"source": "familysearch", "transport": "api", "apid": args.apid, "frame": frame,
                  "records": [{"rid": rid, **fetch_record(client, rid)} for rid in frame["rids"]],
                  "coverage": {"complete": True, "scope": "one-frame-index-only"}}
    else:
        records, exhausted = [], False
        for page in range(args.pages):
            query = {"count": 75, "offset": page * 75}
            for field, value in (("q.surname", args.surname), ("q.givenName", args.given), ("q.anyPlace", args.place)):
                if value:
                    query[field] = value
            data = client.request("/service/search/hr/v2/personas?" + urllib.parse.urlencode(query))
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise SourceError("Некорректный ответ поиска по имени.")
            if not data["entries"]:
                exhausted = True
                break
            for entry in data["entries"]:
                record = (entry.get("content") or {}).get("gedcomx")
                records.append({"rid": entry.get("id"), **parse_record(record), "raw": record, "captured_at": timestamp()})
        result = {"source": "familysearch", "transport": "api", "records": records,
                  "query": {"surname": args.surname, "given": args.given, "place": args.place},
                  "page_limit": args.pages, "search_exhausted": exhausted,
                  "coverage": {"complete": False, "scope": "bounded-name-search"}}
    output_json(result, args.out)
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (SourceError, OSError, ValueError, TypeError, KeyError) as exc:
        # Never print credential-file contents or untrusted source payloads.
        message = str(exc) if isinstance(exc, SourceError) else "Некорректный входной файл или ошибка локального чтения/записи."
        token = os.environ.get("FS_TOKEN", "")
        if token:
            message = message.replace(token, "[session]")
        print("Ошибка: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
