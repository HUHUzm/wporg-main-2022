"""DeepSeek client. Prompts and replies are returned to the caller only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import os
from pathlib import Path
from threading import Lock
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/toolkit.json").read_text())
BASE = "https://api.deepseek.com"
FLASH = CONFIG["default_model"]
PRO = CONFIG["advanced_model"]
CATEGORIES = {
    **dict.fromkeys(("translation", "extraction", "draft", "json", "basic-code",
                     "classification", "formatting"), FLASH),
    **dict.fromkeys(("business-logic", "analysis", "review", "reasoning",
                     "comparison"), PRO),
}
MODELS = {FLASH, PRO}
RETRYABLE = {408, 429, 500, 502, 503, 504}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def now():
    return datetime.now(timezone.utc).isoformat()


def delay(value, attempt):
    if value:
        try:
            return max(0, int(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) -
                               datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return 2 ** attempt


class Tracker:
    def __init__(self):
        self.lock = Lock()
        self.active = self.peak = 0

    def start(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            return now()

    def finish(self):
        with self.lock:
            self.active -= 1
            return now()


def request(path, payload=None, *, timeout=60, tracker=None):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None, None, [], "missing_credentials"
    tracker = tracker or Tracker()
    opener = build_opener(NoRedirect())
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(BASE + path, data=data, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
    })
    attempts = []
    for index in range(3):
        started = tracker.start()
        began = time.monotonic()
        status = body = retry_after = None
        error = None
        try:
            with opener.open(req, timeout=timeout) as response:
                status = response.status
                body = json.load(response)
        except HTTPError as exc:
            status = exc.code
            retry_after = exc.headers.get("Retry-After")
            exc.close()
            error = "http_error"
        except (OSError, HTTPException):
            error = "transport_error"
        except (ValueError, UnicodeError):
            error = "invalid_response"
        finally:
            attempts.append({"started_at": started, "ended_at": tracker.finish(),
                             "duration_ms": round((time.monotonic() - began) * 1000),
                             "http_status": status, "error": error})
        if index == 2 or (status not in RETRYABLE and error != "transport_error"):
            return status, body, attempts, error
        time.sleep(delay(retry_after if status == 429 else None, index))


def list_models():
    status, body, attempts, error = request("/models")
    models = sorted({entry["id"] for entry in body.get("data", [])
                     if isinstance(entry, dict) and entry.get("id") in MODELS}
                    ) if isinstance(body, dict) and isinstance(body.get("data"), list) else []
    return {"http_status": status, "models": models, "missing": sorted(MODELS - set(models)),
            "attempts": attempts, "error": error}


def usage_count(usage, key):
    value = usage.get(key)
    return value if type(value) is int and 0 <= value < 2**63 else None


def run_task(task, context=None, model=None, *, category="general", tracker=None):
    began = time.monotonic()
    category = category if isinstance(category, str) and category in CATEGORIES else "general"
    model = CATEGORIES.get(category, FLASH) if model is None else model
    status, attempts, usage, result, error = None, [], {}, None, None
    if not isinstance(model, str) or model not in MODELS:
        model, error = None, "model_unavailable"
    elif not isinstance(task, str) or not task.strip() or (
        context is not None and not isinstance(context, str)
    ):
        error = "invalid_task"
    else:
        content = task if context is None else f"{task}\n\nContext:\n{context}"
        status, body, attempts, error = request("/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": content}],
        }, tracker=tracker)
        if error is None:
            try:
                result = body["choices"][0]["message"]["content"]
                if not isinstance(result, str):
                    raise TypeError
                key = os.environ.get("DEEPSEEK_API_KEY")
                if key:
                    result = result.replace(key, "[REDACTED]")
                usage = body.get("usage") or {}
                if not isinstance(usage, dict):
                    usage = {}
            except (KeyError, IndexError, TypeError):
                result, error = None, "invalid_response"
    return {"task_id": str(uuid4()), "timestamp": now(), "model": model,
            "task_category": category, "http_status": status,
            "duration_ms": round((time.monotonic() - began) * 1000),
            "input_tokens": usage_count(usage, "prompt_tokens"),
            "output_tokens": usage_count(usage, "completion_tokens"),
            "cache_hit_tokens": usage_count(usage, "prompt_cache_hit_tokens"),
            "cache_miss_tokens": usage_count(usage, "prompt_cache_miss_tokens"),
            "retry_count": max(0, len(attempts) - 1), "attempts": attempts,
            "execution_status": "succeeded" if error is None else "failed",
            "result": result, "error": error}


def run_batch(tasks):
    began = time.monotonic()
    tracker = Tracker()
    batch_id = str(uuid4())

    def execute(item):
        if isinstance(item, str):
            return run_task(item, tracker=tracker)
        if not isinstance(item, dict):
            return run_task(None, tracker=tracker)
        return run_task(item.get("task"), item.get("context"), item.get("model"),
                        category=item.get("category"), tracker=tracker)

    with ThreadPoolExecutor(max_workers=CONFIG["max_concurrency"]) as pool:
        results = list(pool.map(execute, tasks))
    for result in results:
        result["batch_id"] = batch_id
    return {"batch_id": batch_id, "results": results, "peak_concurrency": tracker.peak,
            "duration_ms": round((time.monotonic() - began) * 1000),
            **{key: sum(result[key] or 0 for result in results)
               for key in ("input_tokens", "output_tokens", "retry_count")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--task")
    mode.add_argument("--batch", type=Path)
    mode.add_argument("--models", action="store_true")
    parser.add_argument("--context")
    parser.add_argument("--model", choices=sorted(MODELS))
    parser.add_argument("--category", choices=[*sorted(CATEGORIES), "general"], default="general")
    parser.add_argument("--report", type=Path, help="Write the result JSON to a new file")
    parser.add_argument("--db", type=Path, default=Path(".agent-toolkit/monitor.sqlite3"),
                        help="SQLite execution metadata path")
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()
    if args.models:
        output = list_models()
        failed = bool(output["error"] or output["missing"])
        results = []
    elif args.batch:
        try:
            tasks = json.loads(args.batch.read_text())
        except (OSError, ValueError):
            parser.error("batch must be a readable JSON file")
        if not isinstance(tasks, list):
            parser.error("batch must contain an array")
        output = run_batch(tasks)
        results = output["results"]
        failed = any(item["error"] for item in results)
    else:
        output = run_task(args.task, args.context, args.model, category=args.category)
        results = [output]
        failed = bool(output["error"])
    if args.db and results:
        from workers.monitor import record_execution
        failures = sum(not record_execution(args.db, item, args.project_id) for item in results)
        if failures:
            import sys
            print(f"Monitoring writes failed: {failures}", file=sys.stderr)
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.report:
        with os.fdopen(os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                       "w", encoding="utf-8") as report:
            report.write(rendered + "\n")
    print(rendered)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
