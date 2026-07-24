import os


def _database_uri():
    """Vercel Postgres/Neon integrations set one of a few possible env var names
    depending on which storage product was attached. Check them in priority order,
    falling back to local SQLite so `python run.py` keeps working with zero env vars."""
    uri = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("POSTGRES_PRISMA_URL")
    )
    if uri:
        # Vercel/Neon URLs commonly use the "postgres://" scheme; SQLAlchemy 2.x with
        # psycopg3 needs the "postgresql+psycopg://" dialect prefix.
        if uri.startswith("postgres://"):
            uri = uri.replace("postgres://", "postgresql+psycopg://", 1)
        elif uri.startswith("postgresql://"):
            uri = uri.replace("postgresql://", "postgresql+psycopg://", 1)
        return uri
    return "sqlite:///commissions.db"


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-in-production")
    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB

    # Secure cookies require HTTPS — only enforced when FLASK_DEBUG is off (production),
    # so local HTTP dev isn't broken by browsers silently dropping Secure cookies.
    SESSION_COOKIE_SECURE = os.environ.get("FLASK_DEBUG", "1") != "1"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
