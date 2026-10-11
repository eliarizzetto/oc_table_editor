"""Shared fixtures for the OC Table Editor test suite.

Every test runs against a throwaway ``TEMP_DIR``: the app resolves all
session paths through the ``TEMP_DIR`` attribute imported (by value) by
``config``, ``services.journal``, ``services.session_manager``,
``services.view_builder`` and ``routes.edit`` — patching those five
bindings redirects the whole app into ``tmp_path``, so nothing ever
touches the repo's real ``temp/`` directory.
"""
import pytest

from test.support import (CITS_INVALID_CSV, META_INVALID_CSV, META_MIXED_CSV,
                          META_VALID_CSV, upload_csvs)

__all__ = [
    "temp_env",
    "client",
    "invalid_meta_session",
    "valid_meta_session",
    "mixed_meta_session",
    "paired_session",
    "upload_csvs",
]


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Redirect every module-level ``TEMP_DIR`` binding to ``tmp_path``."""
    import config
    import routes.edit as edit_mod
    import services.journal as journal_mod
    import services.session_manager as session_manager_mod
    import services.view_builder as view_builder_mod

    for mod in (config, journal_mod, session_manager_mod, view_builder_mod,
                edit_mod):
        monkeypatch.setattr(mod, "TEMP_DIR", tmp_path)
    view_builder_mod._STATE_CACHE.clear()
    yield tmp_path
    view_builder_mod._STATE_CACHE.clear()


@pytest.fixture
def client():
    """A synchronous TestClient around the FastAPI app."""
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def invalid_meta_session(client, temp_env):
    """An uploaded 3-row invalid metadata session (one error per row)."""
    return upload_csvs(client, meta=META_INVALID_CSV,
                       meta_name="invalid_metadata.csv")


@pytest.fixture
def valid_meta_session(client, temp_env):
    """An uploaded 2-row fully valid metadata session."""
    return upload_csvs(client, meta=META_VALID_CSV,
                       meta_name="valid_metadata.csv")


@pytest.fixture
def mixed_meta_session(client, temp_env):
    """An uploaded metadata session whose row 2 ('Good Row') has no issues."""
    return upload_csvs(client, meta=META_MIXED_CSV, meta_name="mixed_metadata.csv")


@pytest.fixture
def paired_session(client, temp_env):
    """An uploaded metadata + citations session (both tables editable)."""
    return upload_csvs(client, meta=META_VALID_CSV, cits=CITS_INVALID_CSV,
                       meta_name="m.csv", cits_name="c.csv")
