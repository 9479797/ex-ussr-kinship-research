#!/usr/bin/env python3
"""Create and track institutional requests (archives, ZAGS, government portals) inside an existing case."""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from research_common import SourceError, atomic_bytes, configure_utf8_stdio, outside_skill, state_lock, timestamp

STATUSES = ("draft", "approved", "sent", "registered", "answered", "declined", "closed")
CHANNELS = ("email", "portal", "web-form", "post", "in-person")
MODES = ("online", "hybrid", "offline")
CHANNEL_MODE = {"email": "online", "portal": "online", "web-form": "online", "post": "offline", "in-person": "offline"}
REGISTER = "request-register.csv"
TEMPLATE = "request.template.md"
FIELDS = [
    "request_id", "created_at", "status", "addressee", "channel", "mode", "service_url", "target_person_ids",
    "purpose", "related_claim_ids", "user_authorization", "attachments", "date_sent", "tracking_number",
    "next_check", "reply_artifact_ids", "file", "notes",
]


def split_ids(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;,\s]+", value or "") if item.strip()]


def read_register(path: Path, id_field: str) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise SourceError(f"Нет файла {path.name}; продолжайте существующее дело или создайте его через init_case.py.")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        if not fields or id_field not in fields:
            raise SourceError(f"Некорректный реестр {path.name}.")
        return list(fields), list(reader)


def save_register(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_bytes(path, stream.getvalue().encode("utf-8"))


def next_id(rows: list[dict[str, str]], field: str, prefix: str) -> str:
    numbers = [int(m.group(1)) for row in rows if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", row.get(field, "")))]
    return f"{prefix}{max(numbers, default=0) + 1:04d}"


def known_ids(case: Path, filename: str, field: str) -> set[str]:
    path = case / filename
    if not path.is_file():
        return set()
    _, rows = read_register(path, field)
    return {row[field] for row in rows if row.get(field)}


def check_ids(label: str, ids: list[str], known: set[str]) -> None:
    unknown = [item for item in ids if item not in known]
    if unknown:
        raise SourceError(f"Неизвестные {label}: {', '.join(unknown)}. Сначала зарегистрируйте их в реестре.")


def slugify(value: str) -> str:
    value = re.sub(r"[^\w]+", "-", value, flags=re.UNICODE).strip("-").lower()
    return value[:40].rstrip("-")


def iso_date(value: str, flag: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise SourceError(f"{flag}: ожидается дата в формате YYYY-MM-DD, получено {value!r}.") from exc


def new_request(args: argparse.Namespace) -> dict[str, str]:
    case = outside_skill(args.case_dir)
    template_path = Path(__file__).resolve().parent.parent / "assets" / TEMPLATE
    if not template_path.is_file():
        raise SourceError(f"В навыке отсутствует шаблон {TEMPLATE}.")
    if args.channel not in CHANNELS:
        raise SourceError(f"Канал {args.channel!r} не поддерживается; допустимо: {', '.join(CHANNELS)}.")
    mode = check_mode(args.mode) or CHANNEL_MODE[args.channel]
    persons = split_ids(args.persons)
    claims = split_ids(args.claims)
    if not args.purpose.strip() or not args.addressee.strip():
        raise SourceError("Укажите непустые --purpose и --addressee.")
    check_ids("person_id", persons, known_ids(case, "person-register.csv", "person_id"))
    check_ids("claim_id", claims, known_ids(case, "claims-register.csv", "claim_id"))

    with state_lock(case / ".draft-request.lock"):
        register_path = case / REGISTER
        fields, rows = read_register(register_path, "request_id")
        request_id = next_id(rows, "request_id", "RQ-")
        created_at = timestamp()
        requests_dir = case / "requests"
        requests_dir.mkdir(exist_ok=True)
        slug = slugify(args.slug or args.purpose) or "request"
        file_path = requests_dir / f"{request_id}-{slug}.md"
        if file_path.exists():
            raise SourceError(f"Файл уже существует: {file_path}")
        row = {field: "" for field in fields}
        row.update({
            "request_id": request_id,
            "created_at": created_at,
            "status": "draft",
            "addressee": args.addressee.strip(),
            "channel": args.channel,
            "mode": mode,
            "service_url": args.service_url or "",
            "target_person_ids": ";".join(persons),
            "purpose": args.purpose.strip(),
            "related_claim_ids": ";".join(claims),
            "user_authorization": args.authorization or "",
            "attachments": "",
            "file": file_path.relative_to(case).as_posix(),
            "notes": args.note or "",
        })
        text = template_path.read_text(encoding="utf-8")
        for key, value in row.items():
            text = text.replace("{" + key + "}", value or "—")
        atomic_bytes(file_path, text.encode("utf-8"))
        rows.append(row)
        save_register(register_path, fields, rows)
    return row


def check_mode(value: str) -> str:
    if value and value not in MODES:
        raise SourceError(f"Режим {value!r} не поддерживается; допустимо: {', '.join(MODES)}.")
    return value or ""


def replace_table_value(text: str, label: str, value: str) -> str:
    pattern = re.compile(r"^(\| " + re.escape(label) + r" \| ).*?( \|)$", re.MULTILINE)
    return pattern.sub(lambda m: m.group(1) + value + m.group(2), text, count=1)


def update_request(args: argparse.Namespace) -> dict[str, str]:
    case = outside_skill(args.case_dir)
    if args.status and args.status not in STATUSES:
        raise SourceError(f"Статус {args.status!r} не поддерживается; допустимо: {', '.join(STATUSES)}.")
    mode = check_mode(args.mode)
    reply_ids = split_ids(args.reply_artifacts)
    check_ids("artifact_id", reply_ids, known_ids(case, "artifact-register.csv", "artifact_id"))
    date_sent = iso_date(args.date_sent, "--date-sent") if args.date_sent else ""
    next_check = iso_date(args.next_check, "--next-check") if args.next_check else ""

    with state_lock(case / ".draft-request.lock"):
        register_path = case / REGISTER
        fields, rows = read_register(register_path, "request_id")
        matches = [row for row in rows if row.get("request_id") == args.request_id]
        if not matches:
            raise SourceError(f"Запрос {args.request_id} не найден в {REGISTER}.")
        row = matches[0]
        status = args.status or row.get("status", "")
        if status in ("sent", "registered", "answered") and not (date_sent or row.get("date_sent")):
            date_sent = date.today().isoformat()
        if status == "answered" and not (reply_ids or split_ids(row.get("reply_artifact_ids", ""))):
            raise SourceError("Для статуса answered укажите --reply-artifacts с уже зарегистрированным ответом.")
        changes = {
            "status": status,
            "mode": mode or row.get("mode", ""),
            "date_sent": date_sent or row.get("date_sent", ""),
            "tracking_number": args.tracking_number or row.get("tracking_number", ""),
            "next_check": next_check or row.get("next_check", ""),
            "reply_artifact_ids": ";".join(dict.fromkeys(split_ids(row.get("reply_artifact_ids", "")) + reply_ids)),
            "attachments": args.attachments if args.attachments is not None else row.get("attachments", ""),
            "user_authorization": args.authorization or row.get("user_authorization", ""),
        }
        if args.note:
            changes["notes"] = "; ".join(part for part in (row.get("notes", ""), args.note) if part)
        row.update(changes)
        save_register(register_path, fields, rows)

        file_path = case / row.get("file", "")
        if row.get("file") and file_path.is_file():
            text = file_path.read_text(encoding="utf-8")
            text = replace_table_value(text, "Статус", row["status"])
            text = replace_table_value(text, "Режим", row["mode"] or "—")
            text = replace_table_value(text, "Отправлено", row["date_sent"] or "—")
            text = replace_table_value(text, "Номер обращения", row["tracking_number"] or "—")
            text = replace_table_value(text, "Следующая проверка", row["next_check"] or "—")
            text = replace_table_value(text, "Разрешение пользователя", row["user_authorization"] or "—")
            entry = f"- {timestamp()}: {row['status']}"
            if args.note:
                entry += f" — {args.note}"
            text = text.rstrip("\n") + "\n" + entry + "\n"
            atomic_bytes(file_path, text.encode("utf-8"))
    return row


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Черновики и статусы запросов в архивы, ЗАГС и государственные порталы в папке дела. "
        "Скрипт не отправляет ничего: отправка и вход на портал остаются за пользователем или разрешённым инструментом."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="Создать черновик requests/RQ-NNNN-<slug>.md и строку в request-register.csv")
    new.add_argument("--case-dir", required=True)
    new.add_argument("--addressee", required=True, help="Учреждение-хранитель или орган, к которому обращаются")
    new.add_argument("--channel", required=True, help=", ".join(CHANNELS))
    new.add_argument("--purpose", required=True, help="Какой вопрос или родственное звено закрывает ответ")
    new.add_argument("--mode", default="", help=", ".join(MODES) + "; по умолчанию online для email/portal/web-form, offline для post/in-person; hybrid — онлайн-подача с личной явкой за результатом")
    new.add_argument("--service-url", default="", help="Официальная страница услуги или контактов")
    new.add_argument("--persons", default="", help="person_id через запятую")
    new.add_argument("--claims", default="", help="claim_id через запятую")
    new.add_argument("--authorization", default="", help="Что именно разрешил пользователь (или пусто)")
    new.add_argument("--slug", default="", help="Имя файла без ID; по умолчанию из --purpose")
    new.add_argument("--note", default="")

    update = sub.add_parser("update", help="Изменить статус, дату отправки, номер обращения или ответ")
    update.add_argument("--case-dir", required=True)
    update.add_argument("--request-id", required=True)
    update.add_argument("--status", default="", help=", ".join(STATUSES))
    update.add_argument("--mode", default="", help=", ".join(MODES))
    update.add_argument("--date-sent", default="", help="YYYY-MM-DD; для sent/registered/answered по умолчанию сегодня")
    update.add_argument("--tracking-number", default="", help="Номер обращения, ID письма или цепочки")
    update.add_argument("--next-check", default="", help="YYYY-MM-DD из подтверждения учреждения")
    update.add_argument("--reply-artifacts", default="", help="artifact_id ответа через запятую (обязательно для answered)")
    update.add_argument("--attachments", default=None, help="Перечень фактически приложенных документов")
    update.add_argument("--authorization", default="")
    update.add_argument("--note", default="")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        row = new_request(args) if args.command == "new" else update_request(args)
    except SourceError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    print(f"{row['request_id']}: {row['status']} — {row['file']}")
    return 0


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
