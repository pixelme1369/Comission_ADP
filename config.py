import os

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-in-production")
    SQLALCHEMY_DATABASE_URI = "sqlite:///commissions.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB
