import logging
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import KbArticle

logger = logging.getLogger(__name__)

KB_DIR = Path(__file__).resolve().parent.parent / "seed" / "kb"
BASE_URL = "https://help.reporthub.example"


def load_articles(kb_dir: Path = KB_DIR) -> list[dict[str, str]]:
    articles = []
    for path in sorted(kb_dir.glob("*.md")):
        title_line, _, body = path.read_text(encoding="utf-8").partition("\n")
        articles.append(
            {
                "slug": path.stem,
                "title": title_line.removeprefix("# ").strip(),
                "body": body.strip(),
                "url": f"{BASE_URL}/{path.stem}",
            }
        )
    return articles


def seed_articles(session: Session, kb_dir: Path = KB_DIR) -> int:
    articles = load_articles(kb_dir)
    if not articles:
        return 0
    stmt = insert(KbArticle).values(articles)
    stmt = stmt.on_conflict_do_update(
        index_elements=["slug"],
        set_={col: stmt.excluded[col] for col in ("title", "body", "url")},
    )
    session.execute(stmt)
    session.commit()
    logger.info("kb seeded: %d articles upserted", len(articles))
    return len(articles)
