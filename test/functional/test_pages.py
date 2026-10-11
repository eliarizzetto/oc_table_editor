"""Functional tests for the page routes (main.py)."""


class TestHealth:
    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["app"] == "OC Table Editor"


class TestIndexPage:
    def test_index_renders_upload_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "OC Table Editor" in resp.text
        assert "<form" in resp.text


class TestEditorPage:
    def test_editor_page_for_existing_session(self, client,
                                              invalid_meta_session):
        resp = client.get(f"/editor/{invalid_meta_session}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert invalid_meta_session in resp.text

    def test_editor_page_for_unknown_session_is_404(self, client, temp_env):
        resp = client.get("/editor/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404
        assert "not found" in resp.text or "expired" in resp.text
