import sys
from pathlib import Path

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from config import Config

# commission_core/ (shared calculator.py/crm_parser.py/cordoba_parser.py/
# commission_history_parser.py, used by both this app and agent_portal/) lives
# inside agent_portal/ rather than at the repo root — see
# agent_portal/commission_core/README.md for why. This makes `import
# commission_core` resolve from anywhere in this app's code, same as if it
# were a true repo-root sibling package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_portal"))

db = SQLAlchemy()


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)

    db.init_app(app)

    from app.routes import bp
    app.register_blueprint(bp)

    return app
