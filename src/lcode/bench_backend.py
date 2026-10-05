"""A larger repository for `lcode bench --tasks find-concept`: finding code by what it does.

About forty modules of a small backend. Many of them talk about retries, uploads, waiting and
backoff; the function the task asks about is called `_cooldown_for` and its docstring uses none of
those words. The model has to follow the code from where files are sent to where the pause between
attempts is computed, or find it by meaning.
"""

from __future__ import annotations

import re
from pathlib import Path

PACING = '''"""How long the transport layer holds off between attempts."""

import random

BASE_PAUSE = 0.5
MAX_PAUSE = 30.0


def _cooldown_for(attempt: int) -> float:
    """Seconds to hold off before attempt number `attempt`: doubles each time, with some spread."""
    pause = min(MAX_PAUSE, BASE_PAUSE * (2**attempt))
    return pause * random.uniform(0.8, 1.2)


def pause_budget(attempts: int) -> float:
    """The most time `attempts` pauses can take in all."""
    return sum(min(MAX_PAUSE, BASE_PAUSE * (2**a)) * 1.2 for a in range(attempts))
'''

SENDER = '''"""Sends files to the object store, part by part."""

import time

from storage.checksums import md5_of
from storage.chunking import split_file
from transport.http import put
from transport.pacing import _cooldown_for
from utils.config import RETRY_LIMIT


class TransferFailed(Exception):
    pass


def send_file(bucket: str, path: str) -> int:
    parts = 0
    for number, part in enumerate(split_file(path)):
        send_part(bucket, path, number, part)
        parts += 1
    return parts


def send_part(bucket: str, path: str, number: int, part: bytes) -> dict:
    for attempt in range(RETRY_LIMIT):
        try:
            return put(f"/{bucket}/{path}/{number}", part, checksum=md5_of(part))
        except ConnectionError:
            time.sleep(_cooldown_for(attempt))
    raise TransferFailed(f"{path} part {number}")
'''

FILES = {
    "README.md": "# shopcore\n\nThe backend of a small online shop: orders, billing, uploads and reports.\n",
    "transport/__init__.py": "",
    "transport/pacing.py": PACING,
    "transport/http.py": '''"""A thin HTTP client for the object store and partner APIs."""

import json
import urllib.request

RETRY_POLICY = {"retries": 3, "backoff_factor": 0.3, "retry_on": (502, 503, 504)}
TIMEOUT = 20


def put(url: str, body: bytes, checksum: str = "") -> dict:
    request = urllib.request.Request(url, data=body, method="PUT", headers={"Content-MD5": checksum})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read() or b"{}")


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def retry_delay_header(headers: dict) -> float | None:
    """The delay a server asked for in its Retry-After header (on 429 and 503 answers)."""
    value = headers.get("Retry-After")
    return float(value) if value and value.replace(".", "", 1).isdigit() else None
''',
    "transport/limits.py": '''"""Rate limits for partner APIs."""

import time

CALLS_PER_SECOND = 5
_last_call = 0.0


def throttle() -> None:
    """Wait so that calls stay under CALLS_PER_SECOND."""
    global _last_call
    gap = 1 / CALLS_PER_SECOND - (time.monotonic() - _last_call)
    if gap > 0:
        time.sleep(gap)
    _last_call = time.monotonic()
''',
    "transport/sockets.py": '''"""Keep-alive connections to the object store."""

import socket

POOL_SIZE = 8


def open_connection(host: str, port: int = 443, timeout: float = 10.0) -> socket.socket:
    connection = socket.create_connection((host, port), timeout=timeout)
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    return connection
''',
    "storage/__init__.py": "",
    "storage/sender.py": SENDER,
    "storage/chunking.py": '''"""Splitting files into parts for upload."""

PART_SIZE = 8 * 1024 * 1024


def split_file(path: str, size: int = PART_SIZE):
    with open(path, "rb") as f:
        while part := f.read(size):
            yield part
''',
    "storage/checksums.py": '''"""Checksums the object store verifies."""

import base64
import hashlib


def md5_of(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()
''',
    "storage/buckets.py": '''"""Which bucket things go to."""

BUCKETS = {"invoice": "shop-invoices", "report": "shop-reports", "image": "shop-images"}


def bucket_for(kind: str) -> str:
    return BUCKETS.get(kind, "shop-misc")
''',
    "storage/manifests.py": '''"""A manifest lists the parts of an uploaded file."""

import json


def write_manifest(path: str, parts: int, checksum: str) -> str:
    manifest = {"file": path, "parts": parts, "checksum": checksum}
    target = path + ".manifest.json"
    with open(target, "w") as f:
        json.dump(manifest, f)
    return target
''',
    "jobs/__init__.py": "",
    "jobs/queue.py": '''"""A small job queue backed by the database."""

import time

POLL_INTERVAL = 2.0


def wait_for_job(store, job_id: str, timeout: float = 300.0) -> dict:
    """Poll until the job finishes; keep retrying the lookup until the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(job_id)
''',
    "jobs/workers.py": '''"""Workers run jobs and put failed ones back in the queue."""

REQUEUE_BASE = 60


def requeue_delay(job: dict) -> int:
    """Seconds before a failed job is retried by the worker pool: one more minute per failure."""
    return REQUEUE_BASE * (job.get("retry_count", 0) + 1)


def run_job(job: dict, handlers: dict) -> dict:
    handler = handlers[job["kind"]]
    try:
        return {"state": "done", "result": handler(**job["args"])}
    except Exception as e:
        return {"state": "failed", "error": str(e), "retry_count": job.get("retry_count", 0) + 1}
''',
    "jobs/scheduler.py": '''"""Runs recurring jobs: nightly reports, invoice reminders."""

SCHEDULE = {"nightly_report": "02:00", "invoice_reminders": "09:00", "cleanup_uploads": "03:30"}


def due(now_hhmm: str) -> list[str]:
    return [name for name, at in SCHEDULE.items() if at == now_hhmm]
''',
    "api/__init__.py": "",
    "api/routes_uploads.py": '''"""HTTP routes for uploading product images and documents."""

from storage.buckets import bucket_for
from storage.sender import send_file

MAX_UPLOAD_MB = 50


def upload_image(request) -> dict:
    """Store an uploaded product image; the client may retry with the same upload id."""
    path = request.save_to_temp()
    parts = send_file(bucket_for("image"), path)
    return {"ok": True, "parts": parts, "upload_id": request.headers.get("Upload-Id")}


def upload_status(request) -> dict:
    return {"upload_id": request.args["id"], "state": "stored"}
''',
    "api/routes_orders.py": '''"""HTTP routes for orders."""

from billing.invoices import create_invoice


def place_order(request) -> dict:
    order = request.json()
    invoice = create_invoice(order["customer"], order["items"])
    return {"order": order["id"], "invoice": invoice["number"]}


def cancel_order(request) -> dict:
    return {"order": request.args["id"], "state": "cancelled"}
''',
    "api/routes_users.py": '''"""HTTP routes for accounts."""

from auth.passwords import check_password
from auth.sessions import start_session


def login(request) -> dict:
    user = request.db.find_user(request.json()["email"])
    if not user or not check_password(request.json()["password"], user["hash"]):
        return {"ok": False}
    return {"ok": True, "session": start_session(user["id"])}
''',
    "api/middleware.py": '''"""Request middleware: request ids and timing."""

import time
import uuid


def with_request_id(handler):
    def wrapped(request):
        request.id = str(uuid.uuid4())
        started = time.monotonic()
        response = handler(request)
        response["took_ms"] = round((time.monotonic() - started) * 1000)
        return response

    return wrapped
''',
    "api/errors.py": '''"""Error responses."""


class ApiError(Exception):
    status = 400


class RetryLater(ApiError):
    """Ask the client to retry: 503 with a Retry-After header."""

    status = 503
    retry_after = 30
''',
    "auth/__init__.py": "",
    "auth/passwords.py": '''"""Password hashing."""

import hashlib
import hmac
import os


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + ":" + digest.hex()


def check_password(password: str, stored: str) -> bool:
    salt, digest = stored.split(":")
    again = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return hmac.compare_digest(again.hex(), digest)
''',
    "auth/sessions.py": '''"""Login sessions."""

import secrets
import time

SESSION_SECONDS = 7 * 24 * 3600
_sessions: dict[str, tuple[str, float]] = {}


def start_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = (user_id, time.time() + SESSION_SECONDS)
    return token


def user_for(token: str) -> str | None:
    found = _sessions.get(token)
    return found[0] if found and found[1] > time.time() else None
''',
    "auth/tokens.py": '''"""API tokens for partners."""

import secrets


def new_api_token() -> str:
    return "sk_" + secrets.token_hex(24)
''',
    "billing/__init__.py": "",
    "billing/invoices.py": '''"""Invoices."""

from billing.tax import vat_for
from reports.export import upload_report

_next_number = 1000


def create_invoice(customer: dict, items: list[dict]) -> dict:
    global _next_number
    _next_number += 1
    net = sum(i["price"] * i["quantity"] for i in items)
    invoice = {"number": _next_number, "customer": customer["id"], "net": net, "vat": vat_for(customer, net)}
    upload_report({"kind": "invoice", "data": invoice})
    return invoice
''',
    "billing/tax.py": '''"""VAT."""

RATES = {"DE": 0.19, "NL": 0.21, "FR": 0.20}


def vat_for(customer: dict, net: float) -> float:
    return round(net * RATES.get(customer.get("country", ""), 0.0), 2)
''',
    "billing/payments.py": '''"""Card payments through the payment provider."""

from transport.http import get_json
from transport.limits import throttle


def payment_status(payment_id: str) -> str:
    throttle()
    return get_json(f"https://pay.example.com/v1/payments/{payment_id}")["status"]
''',
    "billing/refunds.py": '''"""Refunds."""

REFUND_DAYS = 30


def refundable(order: dict, days_since: int) -> bool:
    return order["state"] == "delivered" and days_since <= REFUND_DAYS
''',
    "reports/__init__.py": "",
    "reports/export.py": '''"""Exporting reports as files for the accounting team."""

import json
import tempfile

from storage.buckets import bucket_for
from storage.sender import send_file


def upload_report(report: dict) -> int:
    """Write the report to a file and upload it; uploads are retried by the sender."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(report, f)
    return send_file(bucket_for("report"), f.name)
''',
    "reports/charts.py": '''"""Sales charts."""


def daily_totals(orders: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for order in orders:
        totals[order["day"]] = totals.get(order["day"], 0.0) + order["total"]
    return totals
''',
    "utils/__init__.py": "",
    "utils/config.py": '''"""Settings."""

import os

RETRY_LIMIT = int(os.environ.get("SHOP_RETRY_LIMIT", "5"))
UPLOAD_TIMEOUT = 120
LOG_LEVEL = os.environ.get("SHOP_LOG_LEVEL", "INFO")
''',
    "utils/timeutil.py": '''"""Time helpers."""

import time


def backoff_seconds(level: int) -> float:
    """How long to keep quiet before logging the same warning again (repeated warnings back off)."""
    return min(3600.0, 10.0 * (3**level))


def wait_until(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
''',
    "utils/strings.py": '''"""String helpers."""

import re


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
''',
    "utils/ids.py": '''"""Ids."""

import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
''',
    "tests/test_sender.py": """import unittest
from unittest import mock

from storage import sender


class SenderTest(unittest.TestCase):
    def test_gives_up_after_the_limit(self):
        with mock.patch.object(sender, "put", side_effect=ConnectionError), mock.patch("time.sleep"):
            with self.assertRaises(sender.TransferFailed):
                sender.send_part("b", "f", 0, b"x")
""",
    "tests/test_workers.py": """import unittest

from jobs.workers import requeue_delay


class WorkersTest(unittest.TestCase):
    def test_requeue_delay_grows(self):
        self.assertEqual(requeue_delay({"retry_count": 2}), 180)
""",
}

PROMPT = (
    "When sending a part of a file to the object store fails, the client waits a little before sending that "
    "part again. Which function computes how long that wait is? Reply with the function name and where it is "
    "defined, as path:line (for example `pkg/module.py:12`)."
)


def check(folder: Path, answer: str):
    from lcode.bench import Check

    lines = PACING.splitlines()
    start = next(i for i, line in enumerate(lines, 1) if line.startswith("def _cooldown_for"))
    cited = [int(n) for n in re.findall(r"transport/pacing\.py:(\d+)", answer)]
    if "_cooldown_for" not in answer:
        for decoy in ("requeue_delay", "backoff_seconds", "retry_delay_header", "pause_budget", "send_part"):
            if decoy in answer:
                return Check(False, f"named {decoy} instead of _cooldown_for")
        return Check(False, "didn't name _cooldown_for")
    if not any(start <= n <= start + 3 for n in cited):
        return Check(
            False, f"named _cooldown_for but cited {cited or 'no'} line(s) of transport/pacing.py, not {start}"
        )
    return Check(True, f"_cooldown_for at transport/pacing.py:{cited[0]}")
