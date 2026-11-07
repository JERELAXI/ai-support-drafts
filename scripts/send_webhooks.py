"""Send test webhooks to the API and print response stats.

Usage: uv run python scripts/send_webhooks.py --count 500 --duplicates
"""

import argparse
import asyncio
import random
import statistics
import time
import uuid
from collections import Counter

import httpx

DEFAULT_URL = "http://localhost:8000/webhooks/helpdesk"
# reply_url is called by the worker inside compose, hence the service hostname.
REPLY_URL = "http://helpdesk-mock:8001/tickets/{ticket_id}/replies"
DUPLICATE_COPIES = 3
WEBHOOK_DEADLINE_SECONDS = 2.0

MESSAGES = [
    "Як експортувати звіт у PDF?",
    "Не можу вивантажити дані в Excel, що робити?",
    "Як скинути пароль і увімкнути двофакторну автентифікацію?",
    "Як поділитися дашбордом за посиланням?",
    "Які ліміти на моєму тарифі?",
    "Як змінити тарифний план і де подивитись рахунки?",
    "Як імпортувати дані з CSV?",
    "Як налаштувати щотижневу розсилку звітів на пошту?",
    "Як створити API-токен?",
    "Як дати колезі права лише на перегляд?",
]


class Stats:
    def __init__(self) -> None:
        self.statuses: Counter[int] = Counter()
        self.duplicates: Counter[bool] = Counter()
        self.errors: Counter[str] = Counter()
        self.latencies: list[float] = []


async def _post(
    client: httpx.AsyncClient, url: str, body: dict[str, str], sem: asyncio.Semaphore, stats: Stats
) -> None:
    async with sem:
        started = time.perf_counter()
        try:
            response = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            stats.errors[type(exc).__name__] += 1
            return
        stats.latencies.append(time.perf_counter() - started)
    stats.statuses[response.status_code] += 1
    if response.status_code == 202:
        stats.duplicates[response.json()["duplicate"]] += 1


async def _send_event(
    client: httpx.AsyncClient,
    url: str,
    run_id: str,
    n: int,
    copies: int,
    sem: asyncio.Semaphore,
    stats: Stats,
) -> None:
    ticket_id = f"T-{run_id}-{n}"
    body = {
        "event_id": f"evt-{run_id}-{n}",
        "ticket_id": ticket_id,
        "customer_email": f"customer{n}@example.com",
        "message": random.choice(MESSAGES),
        "reply_url": REPLY_URL.format(ticket_id=ticket_id),
    }
    # Copies of one event go out at the same time, as a helpdesk redelivery race would.
    await asyncio.gather(*(_post(client, url, body, sem, stats) for _ in range(copies)))


def _percentile(sorted_values: list[float], p: float) -> float:
    index = min(len(sorted_values) - 1, round(p / 100 * (len(sorted_values) - 1)))
    return sorted_values[index]


def _report(stats: Stats, events: int, copies: int, elapsed: float) -> None:
    total = events * copies
    print(f"events: {events}, requests: {total} ({copies} per event)")
    print(f"elapsed: {elapsed:.2f} s, {total / elapsed:.1f} req/s")
    print("status codes:", dict(sorted(stats.statuses.items())) or "none")
    print(f"accepted new: {stats.duplicates[False]}, duplicates: {stats.duplicates[True]}")
    if stats.errors:
        print("errors:", dict(stats.errors))
    if stats.latencies:
        lat = sorted(stats.latencies)
        slow = sum(1 for x in lat if x > WEBHOOK_DEADLINE_SECONDS)
        print(
            f"latency ms: min {lat[0] * 1000:.0f}, p50 {statistics.median(lat) * 1000:.0f}, "
            f"p95 {_percentile(lat, 95) * 1000:.0f}, max {lat[-1] * 1000:.0f}"
        )
        print(f"slower than {WEBHOOK_DEADLINE_SECONDS:.0f} s: {slow}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=10, help="number of distinct events")
    parser.add_argument(
        "--duplicates",
        action="store_true",
        help=f"send every event {DUPLICATE_COPIES} times concurrently",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="webhook URL")
    parser.add_argument(
        "--concurrency", type=int, default=50, help="max requests in flight (default 50)"
    )
    args = parser.parse_args()

    copies = DUPLICATE_COPIES if args.duplicates else 1
    run_id = uuid.uuid4().hex[:8]
    sem = asyncio.Semaphore(args.concurrency)
    stats = Stats()
    limits = httpx.Limits(max_connections=args.concurrency)

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=10, limits=limits) as client:
        await asyncio.gather(
            *(
                _send_event(client, args.url, run_id, n, copies, sem, stats)
                for n in range(args.count)
            )
        )
    _report(stats, args.count, copies, time.perf_counter() - started)
    print(f"run id: {run_id} (event_id prefix evt-{run_id}-)")


if __name__ == "__main__":
    asyncio.run(main())
