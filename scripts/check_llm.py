"""Manual checks for the LLM client and the rate limiter.

python -m scripts.check_llm draft "Не можу експортувати звіт у PDF"
python -m scripts.check_llm ratelimit   # run with LLM_RATE_LIMIT=3
"""

import logging
import sys
import time

from app.config import get_settings
from app.db import SyncSessionLocal
from app.kb import search_articles_sync
from app.llm import generate_draft
from app.rate_limit import WINDOW_SECONDS, acquire


def check_draft(message: str) -> None:
    with SyncSessionLocal() as session:
        articles = search_articles_sync(session, message)
    print(f"found articles: {[a.slug for a in articles]}")
    started = time.monotonic()
    result = generate_draft(message, articles)
    print(f"elapsed: {time.monotonic() - started:.1f}s")
    print(f"sources: {result.sources}")
    print(f"draft:\n{result.draft}")


def check_ratelimit(calls: int = 4) -> None:
    if WINDOW_SECONDS - time.time() % WINDOW_SECONDS < 2:
        time.sleep(2)  # keep all calls inside one window
    print(f"limit: {get_settings().llm_rate_limit}")
    for i in range(1, calls + 1):
        print(f"call {i}: {acquire()}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "draft":
        check_draft(" ".join(sys.argv[2:]) or "Не можу експортувати звіт у PDF")
    elif command == "ratelimit":
        check_ratelimit()
    else:
        sys.exit(__doc__)
