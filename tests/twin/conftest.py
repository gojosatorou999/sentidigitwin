import pytest

from .twin_test_host import create_app, db as _db


@pytest.fixture()
def app(tmp_path, monkeypatch):
    # Redirect the ingest disk cache into the test's own tmp_path.
    #
    # IngestAdapter reads twin.config.CACHE_DIR at call time, and without
    # this every test that runs an adapter -- including the ones that stub a
    # provider with two fake rows -- writes that stub into the real
    # data/twin/cache and it is served to the running app until its TTL
    # expires. That is a week for the camera and street-imagery sources.
    from twin import config as twin_config
    monkeypatch.setattr(twin_config, "CACHE_DIR", str(tmp_path / "twin-cache"))

    # Off by default for the same reason the cache is redirected above: the
    # reference feed's whole job is to fetch a *foreign* authority's catalog
    # when the local one is empty, and every test city here is one with no
    # local coverage. Left on, it turns much of this suite into a live
    # network call against Hong Kong's Transport Department -- which it did,
    # for two tests, before this line existed. The tests that exercise it
    # (tests/twin/test_cctv_reference.py) opt back in with a stubbed
    # provider, so the behaviour is still covered without the network.
    monkeypatch.setattr(twin_config, "CCTV_REFERENCE_ENABLED", False)

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
