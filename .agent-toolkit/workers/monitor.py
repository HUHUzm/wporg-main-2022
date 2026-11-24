"""SQLite execution and review metadata. Task content is never persisted."""
import argparse
from contextlib import contextmanager
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from uuid import UUID

from workers.deepseek_worker import CATEGORIES, CONFIG, MODELS, ROOT, now

TASK_FIELDS = (
    "task_id", "batch_id", "timestamp", "model", "task_category", "http_status",
    "duration_ms", "input_tokens", "output_tokens", "cache_hit_tokens",
    "cache_miss_tokens", "retry_count", "execution_status",
)
SCHEMA_1 = (
    """CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, batch_id TEXT, timestamp TEXT NOT NULL,
        model TEXT, task_category TEXT NOT NULL, http_status INTEGER,
        duration_ms INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
        cache_hit_tokens INTEGER, cache_miss_tokens INTEGER,
        retry_count INTEGER NOT NULL, execution_status TEXT NOT NULL,
        project_id TEXT NOT NULL, toolkit_version TEXT NOT NULL,
        parent_agent TEXT NOT NULL, worker_agent TEXT NOT NULL,
        review_status TEXT, final_success INTEGER)""",
    """CREATE TABLE attempts (
        task_id TEXT NOT NULL REFERENCES tasks(task_id), attempt INTEGER NOT NULL,
        started_at TEXT NOT NULL, ended_at TEXT NOT NULL, http_status INTEGER,
        duration_ms INTEGER NOT NULL, PRIMARY KEY(task_id, attempt))""",
    """CREATE TABLE reviews (
        task_id TEXT NOT NULL REFERENCES tasks(task_id), stage TEXT NOT NULL,
        timestamp TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(task_id, stage))""",
    "CREATE INDEX tasks_timestamp ON tasks(timestamp)",
)
MIGRATIONS = {1: SCHEMA_1}
STAGES = {"initial": ("passed", "failed"), "revision": ("passed", "failed"),
          "takeover": ("taken_over",), "final": ("succeeded", "failed")}


@contextmanager
def database(path, *, write=False):
    path = Path(path)
    if path == Path(".agent-toolkit/monitor.sqlite3"):
        path.parent.mkdir(mode=0o700, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    db = sqlite3.connect(path, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            target = CONFIG["schema_version"]
            if version > target:
                raise ValueError("unsupported database version")
            if version < target:
                if not write:
                    db.rollback()
                    db.execute("BEGIN IMMEDIATE")
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if version > target:
                        raise ValueError("unsupported database version")
                if version == 0 and db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone():
                    raise ValueError("unrecognized unversioned database")
                for level in range(version + 1, target + 1):
                    for statement in MIGRATIONS[level]:
                        db.execute(statement)
                    db.execute(f"PRAGMA user_version = {level}")
            yield db
    finally:
        db.close()


def timestamp(value):
    return datetime.fromisoformat(value).isoformat()


def metadata(result):
    values = {key: result.get(key) for key in TASK_FIELDS}
    for name in ("task_id", "batch_id"):
        if name == "task_id" or values[name] is not None:
            values[name] = str(UUID(values[name]))
    values["timestamp"] = timestamp(values["timestamp"])
    if values["model"] is not None and values["model"] not in MODELS:
        raise ValueError("invalid model")
    if values["task_category"] not in CATEGORIES and values["task_category"] != "general":
        raise ValueError("invalid task category")
    if values["execution_status"] not in ("succeeded", "failed"):
        raise ValueError("invalid execution status")
    for name in ("http_status", "duration_ms", "input_tokens", "output_tokens",
                 "cache_hit_tokens", "cache_miss_tokens", "retry_count"):
        value = values[name]
        if value is not None and (type(value) is not int or not 0 <= value < 2**63):
            raise ValueError("invalid numeric metadata")
    return values


def record_execution(path, result, project_id="default"):
    try:
        secret = os.environ.get("DEEPSEEK_API_KEY")
        if secret and secret in project_id:
            return False
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", project_id):
            return False
        safe = metadata(result)
        with database(path, write=True) as db:
            fields = (*TASK_FIELDS, "project_id", "toolkit_version", "parent_agent", "worker_agent")
            values = [safe[key] for key in TASK_FIELDS]
            values.extend((project_id, CONFIG["toolkit_version"], "Codex", "DeepSeek"))
            db.execute(f"INSERT INTO tasks ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                       values)
            for index, attempt in enumerate(result["attempts"], 1):
                status, duration = attempt["http_status"], attempt["duration_ms"]
                if ((status is not None and (type(status) is not int or not 100 <= status <= 599))
                        or type(duration) is not int or not 0 <= duration < 2**63):
                    raise ValueError("invalid attempt metadata")
                db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?)",
                           (safe["task_id"], index, timestamp(attempt["started_at"]),
                            timestamp(attempt["ended_at"]), status, duration))
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        return False


def record_review(path, task_id, stage, status):
    if status not in STAGES.get(stage, ()):
        raise ValueError("invalid review stage or status")
    task_id = str(UUID(task_id))
    with database(path, write=True) as db:
        db.execute("INSERT INTO reviews VALUES (?, ?, ?, ?)", (task_id, stage, now(), status))
        if stage == "final":
            db.execute("UPDATE tasks SET final_success = ? WHERE task_id = ?",
                       (int(status == "succeeded"), task_id))
        else:
            db.execute("UPDATE tasks SET review_status = ? WHERE task_id = ?",
                       (f"{stage}_{status}", task_id))


def filters(args):
    terms, values = [], []
    if args.date:
        terms.append("substr(t.timestamp, 1, 10) = ?")
        values.append(args.date.isoformat())
    for field in ("model", "project_id", "task_category", "execution_status", "review_status"):
        value = getattr(args, field)
        if value:
            terms.append(f"t.{field} = ?")
            values.append(value)
    return (" WHERE " + " AND ".join(terms) if terms else ""), values


def fraction(numerator, denominator):
    return numerator / denominator if denominator else None


def cost(row, pricing):
    try:
        rates = pricing[row["model"]]
        currency = rates["currency"]
        if not re.fullmatch(r"[A-Z]{3}", currency):
            return None
        if row["cache_hit_tokens"] is not None and row["cache_miss_tokens"] is not None:
            components = ("cache_hit", "cache_miss", "output")
        elif row["cache_hit_tokens"] is None and row["cache_miss_tokens"] is None:
            components = ("input", "output")
        else:
            return None
        amount = Decimal(0)
        for name in components:
            count = row[name + "_tokens"]
            if count is None:
                return None
            rate = Decimal(str(rates[name + "_per_million"]))
            if not rate.is_finite() or rate < 0:
                return None
            amount += count * rate / 1_000_000
        return {"amount": str(amount), "currency": currency}
    except (KeyError, TypeError, InvalidOperation):
        return None


def stats(rows, attempts, reviews):
    attempts_with_status = [row for row in attempts if row["http_status"] is not None]
    initial = [r for r in reviews if r["stage"] == "initial"]
    revisions = [r for r in reviews if r["stage"] == "revision"]
    final = [r["final_success"] for r in rows if r["final_success"] is not None]
    summary = {
        "tasks": len(rows),
        "http_attempts_with_response": len(attempts_with_status),
        "http_success_rate": fraction(sum(200 <= r["http_status"] < 300
                                          for r in attempts_with_status),
                                      len(attempts_with_status)),
        "initial_review_pass_rate": fraction(sum(r["status"] == "passed" for r in initial),
                                             len(initial)),
        "revision_count": len(revisions),
        "revision_pass_rate": fraction(sum(r["status"] == "passed" for r in revisions),
                                        len(revisions)),
        "takeover_count": sum(r["stage"] == "takeover" for r in reviews),
        "finalized_tasks": len(final),
        "final_task_success_rate": fraction(sum(final), len(final)),
        "average_duration_ms": fraction(sum(r["duration_ms"] for r in rows), len(rows)),
    }
    for period, width in (("daily", 10), ("monthly", 7)):
        totals = {}
        for row in rows:
            for field in ("input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens"):
                bucket = totals.setdefault(row["timestamp"][:width], {})
                if row[field] is not None:
                    bucket[field] = bucket.get(field, 0) + row[field]
        summary[period + "_tokens"] = totals
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".agent-toolkit/monitor.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("recent", "attempts", "stats", "export", "reviews"):
        command = commands.add_parser(name)
        command.add_argument("--date", type=date.fromisoformat)
        command.add_argument("--model", choices=sorted(MODELS))
        command.add_argument("--project-id")
        command.add_argument("--category", dest="task_category",
                             choices=[*sorted(CATEGORIES), "general"])
        command.add_argument("--status", dest="execution_status", choices=("succeeded", "failed"))
        command.add_argument("--review-status")
        if name == "recent":
            command.add_argument("--limit", type=int, default=10)
        if name == "export":
            command.add_argument("--format", choices=("json", "csv"), default="json")
    review = commands.add_parser("review")
    review.add_argument("task_id")
    review.add_argument("stage", choices=STAGES)
    review.add_argument("status")
    args = parser.parse_args()
    try:
        if args.command == "review":
            record_review(args.db, args.task_id, args.stage, args.status)
            return
        where, values = filters(args)
        if args.command == "recent" and args.limit < 1:
            parser.error("--limit must be positive")
        with database(args.db) as db:
            rows = [dict(r) for r in db.execute("SELECT t.* FROM tasks t" + where +
                    " ORDER BY t.timestamp DESC" + (" LIMIT ?" if args.command == "recent" else ""),
                    [*values, args.limit] if args.command == "recent" else values)]
            attempts = [dict(r) for r in db.execute(
                "SELECT a.* FROM attempts a JOIN tasks t USING(task_id)" + where +
                " ORDER BY a.started_at, a.attempt", values
            )] if args.command in ("attempts", "stats") else []
            reviews = [dict(r) for r in db.execute(
                "SELECT r.* FROM reviews r JOIN tasks t USING(task_id)" + where +
                " ORDER BY r.timestamp", values
            )] if args.command in ("reviews", "stats") else []
        if args.command == "stats":
            output = stats(rows, attempts, reviews)
        elif args.command in ("attempts", "reviews"):
            output = attempts if args.command == "attempts" else reviews
        else:
            pricing = json.loads((ROOT / "config/prices.json").read_text())
            for row in rows:
                estimate = cost(row, pricing)
                row["estimated_cost"] = estimate["amount"] if estimate else None
                row["estimated_cost_currency"] = estimate["currency"] if estimate else None
            output = rows
        if args.command == "export" and args.format == "csv":
            fields = (*TASK_FIELDS, "project_id", "toolkit_version", "parent_agent", "worker_agent",
                      "review_status", "final_success", "estimated_cost", "estimated_cost_currency")
            writer = csv.DictWriter(sys.stdout, fieldnames=fields)
            writer.writeheader()
            writer.writerows(output)
        else:
            print(json.dumps(output, indent=2))
    except (OSError, sqlite3.Error, ValueError):
        parser.exit(1, "Monitoring operation failed. Check database path, schema and arguments.\n")


if __name__ == "__main__":
    main()
