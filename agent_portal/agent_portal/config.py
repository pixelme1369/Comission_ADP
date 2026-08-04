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

    # "Sign in with Google" (Google Identity Services button on the login page).
    # OAuth 2.0 Client ID from Google Cloud Console → APIs & Services → Credentials
    # → Create Credentials → OAuth client ID → Web application. No client secret
    # needed — the ID-token flow (see auth.py's /login/google) only ever needs the
    # public client ID. Add the deployed domain (and http://127.0.0.1:5000 for local
    # dev) under "Authorized JavaScript origins" on that client. The button is
    # simply hidden on the login page until this is set.
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
