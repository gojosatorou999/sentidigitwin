import pytest

from dev_app import create_app, db as _db


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        database_uri="sqlite:///" + str(tmp_path / "test_twin.db"))
    application.config.update(TESTING=True)
    with application.app_context():
        _db.create_all()
        from twin.seed import seed_metadata
        seed_metadata(_db)
    yield application
    with application.app_context():
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db(app):
    with app.app_context():
        yield _db


@pytest.fixture()
def anon(app):
    return app.test_client()


@pytest.fixture()
def official(app):
    client = app.test_client()
    client.get("/login?role=official")
    return client


@pytest.fixture()
def analyst(app):
    client = app.test_client()
    client.get("/login?role=analyst")
    return client


@pytest.fixture()
def citizen(app):
    client = app.test_client()
    client.get("/login?role=citizen")
    return client
