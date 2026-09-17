"""技能管理 REST 接口契约。"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr(s, "skills_enabled", True)
    from app.main import app

    return TestClient(app)


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_create_list_get(client):
    r = client.post("/api/v1/skills", json={"name": "营收口径", "description": "统一口径", "body": "正文"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    lst = client.get("/api/v1/skills").json()
    assert lst["total"] == 1
    assert lst["skills"][0]["id"] == sid

    detail = client.get(f"/api/v1/skills/{sid}").json()
    assert detail["body"].strip() == "正文"


def test_create_empty_name_is_422(client):
    assert client.post("/api/v1/skills", json={"name": ""}).status_code == 422


def test_update_and_enabled_and_delete(client):
    sid = client.post("/api/v1/skills", json={"name": "甲", "body": "旧"}).json()["id"]

    r = client.put(f"/api/v1/skills/{sid}", json={"body": "新"})
    assert r.status_code == 200
    assert r.json()["body"].strip() == "新"

    r = client.post(f"/api/v1/skills/{sid}/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    assert client.delete(f"/api/v1/skills/{sid}").json()["ok"] is True
    assert client.get(f"/api/v1/skills/{sid}").status_code == 404


def test_unknown_id_404(client):
    assert client.get("/api/v1/skills/nope").status_code == 404
    assert client.put("/api/v1/skills/nope", json={"body": "x"}).status_code == 404
    assert client.delete("/api/v1/skills/nope").status_code == 404
    assert client.post("/api/v1/skills/nope/enabled", json={"enabled": True}).status_code == 404


def test_import_zip(client):
    data = _zip({"s/SKILL.md": "---\nname: 导入技能\ndescription: d\n---\n正文"})
    r = client.post(
        "/api/v1/skills/import",
        files={"file": ("skill.zip", data, "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["imported"]) == 1
    assert body["imported"][0]["name"] == "导入技能"
    assert client.get("/api/v1/skills").json()["total"] == 1


def test_import_invalid_zip_is_400(client):
    r = client.post(
        "/api/v1/skills/import",
        files={"file": ("bad.zip", b"not-a-zip", "application/zip")},
    )
    assert r.status_code == 400


def test_disabled_gate_returns_503(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "skills_enabled", False)
    assert client.get("/api/v1/skills").status_code == 503
    assert client.post("/api/v1/skills", json={"name": "x"}).status_code == 503
