import logging

from app.db import SyncSessionLocal
from app.kb_seed import seed_articles

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with SyncSessionLocal() as session:
        seed_articles(session)
