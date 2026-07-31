import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-in-production")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///agent_portal_dev.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB

    # Google Drive sync source — see README for one-time service-account setup.
    GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1YQDdZ1bYTDqgricxO9f_TyDyWredgDzg")  # Cordoba_ADP

    # Shared secret Vercel Cron sends as `Authorization: Bearer <CRON_SECRET>` to
    # GET /cron/sync — that route has no login session to check, so this is what
    # keeps it from being an open "trigger a Drive sync" endpoint on the internet.
    CRON_SECRET = os.environ.get("CRON_SECRET")
