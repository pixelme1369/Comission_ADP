from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)

    db.init_app(app)
    migrate.init_app(app, db)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "error"

    from app.models import AgentUser

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(AgentUser, int(user_id))

    from app.routes import bp
    app.register_blueprint(bp)

    from app.auth_routes import auth_bp
    app.register_blueprint(auth_bp)

    return app
