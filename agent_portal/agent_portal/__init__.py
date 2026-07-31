from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

from agent_portal.config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)

    # Neon/Heroku-style URLs use the "postgres://" scheme; SQLAlchemy 1.4+/psycopg
    # require "postgresql://" — normalize so pasting the raw connection string
    # straight from Neon's dashboard into DATABASE_URL just works.
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("postgres://"):
        app.config["SQLALCHEMY_DATABASE_URI"] = uri.replace("postgres://", "postgresql://", 1)

    db.init_app(app)
    login_manager.init_app(app)

    from agent_portal.models import Agent

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(Agent, int(user_id))

    from agent_portal.auth import bp as auth_bp
    from agent_portal.routes_agent import bp as agent_bp
    from agent_portal.routes_admin import bp as admin_bp, cron_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(agent_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(cron_bp)

    return app
