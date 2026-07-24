import pytest
from werkzeug.security import generate_password_hash

from app import create_app, db as _db
from app.models import AgentUser


@pytest.fixture()
def app():
    """App wired to a throwaway in-memory database."""
    app = create_app({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "TESTING": True,
    })
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def client(app):
    return app.test_client()


def _make_user(db, username, password, agent_name=None, is_admin=False, active=True):
    user = AgentUser(
        username=username,
        password_hash=generate_password_hash(password),
        agent_name=agent_name,
        is_admin=is_admin,
        active=active,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def make_user(db):
    """Factory fixture: make_user(username, password, agent_name=None, is_admin=False, active=True)."""
    def _factory(username, password, **kwargs):
        return _make_user(db, username, password, **kwargs)
    return _factory


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)
