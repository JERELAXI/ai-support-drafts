import os

# Required settings for tests that do not touch the services.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OPERATOR_USER", "operator")
os.environ.setdefault("OPERATOR_PASSWORD", "secret")
