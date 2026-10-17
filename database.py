"""SQLite database configuration and session management for Lunch App."""

from contextlib import contextmanager
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_FILE = os.getenv("LUNCH_APP_DB_PATH", os.path.join(os.path.dirname(__file__), "lunch_app.db"))
DATABASE_URL = f"sqlite:///{DATABASE_FILE}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db():
    """Create all tables in the SQLite database."""
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_db_context():
    """Context manager for standalone database sessions."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    """FastAPI dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
