import pytest

from app import create_app, db as _db


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
