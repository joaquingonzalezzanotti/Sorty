import importlib
import os
import sqlite3
from pathlib import Path

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text


def _sqlite_uri(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _load_app_module(db_uri: str):
    os.environ["DATABASE_URL"] = db_uri
    os.environ["EMAIL_MODE"] = "console"
    os.environ["WHATSAPP_MODE"] = "console"
    module = importlib.import_module("app")
    module = importlib.reload(module)
    module._schema_initialized = False
    return module


def _create_legacy_sorteo_table(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE sorteo (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              public_id TEXT NOT NULL,
              code TEXT NOT NULL,
              nombre VARCHAR(255) NOT NULL,
              email_admin VARCHAR(320) NOT NULL,
              estado VARCHAR(20) NOT NULL DEFAULT 'borrador',
              fecha_creacion DATETIME NOT NULL,
              fecha_expiracion DATETIME NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sorteo
              (public_id, code, nombre, email_admin, estado, fecha_creacion, fecha_expiracion)
            VALUES
              ('legacy-id-1', 'LEGACY01', 'Sorteo legacy', 'legacy@example.com', 'finalizado', '2026-01-01', '2026-01-08')
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_schema_migration_adds_admin_contact_from_legacy_column(tmp_path):
    db_path = tmp_path / "legacy_schema.db"
    _create_legacy_sorteo_table(db_path)
    app_module = _load_app_module(_sqlite_uri(db_path))

    with app_module.app.app_context():
        app_module.ensure_database_schema()
        columns = {col["name"] for col in sa_inspect(app_module.db.engine).get_columns("sorteo")}
        assert "admin_contact" in columns
        migrated_value = app_module.db.session.execute(
            sa_text("SELECT admin_contact FROM sorteo WHERE code = 'LEGACY01'")
        ).scalar_one()
        assert migrated_value == "legacy@example.com"


def test_complex_email_flow_create_get_update_and_resend(tmp_path):
    db_path = tmp_path / "complex_email.db"
    app_module = _load_app_module(_sqlite_uri(db_path))
    client = app_module.app.test_client()

    payload = {
        "channel": "email",
        "mode": "console",
        "send": False,
        "participants": [
            {"name": "Admin", "email": "admin@example.com", "is_admin": True},
            {"name": "Ana", "email": "ana@example.com", "is_admin": False},
            {"name": "Beto", "email": "beto@example.com", "is_admin": False},
        ],
        "exclusions": [{"from": "ana@example.com", "to": "admin@example.com"}],
        "meta": {"budget": "$200", "deadline": "24/12", "note": "Traer envuelto"},
    }

    create_res = client.post("/api/sorteo", json=payload)
    assert create_res.status_code == 200
    create_data = create_res.get_json()
    assert create_data["ok"] is True
    draw_code = create_data["draw_code"]
    assert draw_code

    draw_res = client.get(f"/api/sorteo/{draw_code}")
    assert draw_res.status_code == 200
    draw_data = draw_res.get_json()["draw"]
    assert draw_data["channel"] == "email"
    assert len(draw_data["participants"]) == 3
    assert len(draw_data["assignments"]) == 3

    target = next(item for item in draw_data["participants"] if item["email"] == "ana@example.com")
    update_res = client.patch(
        f"/api/sorteo/{draw_code}/participant/{target['id']}/contact",
        json={"contact": "ana+new@example.com", "notify_previous": False},
    )
    assert update_res.status_code == 200
    update_data = update_res.get_json()
    assert update_data["ok"] is True
    assert update_data["participant"]["email"] == "ana+new@example.com"

    resend_res = client.post(f"/api/sorteo/{draw_code}/resend", json={"mode": "console"})
    assert resend_res.status_code == 200
    resend_data = resend_res.get_json()
    assert resend_data["ok"] is True
    assert resend_data["channel"] == "email"
    assert "Correos" in resend_data["message"]


def test_complex_whatsapp_flow_with_validation_and_resend(tmp_path):
    db_path = tmp_path / "complex_whatsapp.db"
    app_module = _load_app_module(_sqlite_uri(db_path))
    client = app_module.app.test_client()

    payload = {
        "channel": "whatsapp",
        "mode": "console",
        "send": False,
        "participants": [
            {"name": "Admin W", "email": "+5493511111111", "is_admin": True},
            {"name": "Ana W", "email": "+5493512222222", "is_admin": False},
            {"name": "Beto W", "email": "+5493513333333", "is_admin": False},
        ],
        "exclusions": [{"from": "+5493512222222", "to": "+5493511111111"}],
        "meta": {"budget": "$500", "deadline": "10/01", "note": "Sin contar a nadie"},
    }

    create_res = client.post("/api/sorteo", json=payload)
    assert create_res.status_code == 200
    create_data = create_res.get_json()
    draw_code = create_data["draw_code"]
    assert draw_code

    draw_res = client.get(f"/api/sorteo/{draw_code}")
    assert draw_res.status_code == 200
    draw_data = draw_res.get_json()["draw"]
    assert draw_data["channel"] == "whatsapp"
    assert len(draw_data["participants"]) == 3
    assert len(draw_data["assignments"]) == 3

    target = next(item for item in draw_data["participants"] if item["email"] == "+5493512222222")
    invalid_update = client.patch(
        f"/api/sorteo/{draw_code}/participant/{target['id']}/contact",
        json={"contact": "ana@example.com"},
    )
    assert invalid_update.status_code == 400

    valid_update = client.patch(
        f"/api/sorteo/{draw_code}/participant/{target['id']}/contact",
        json={"contact": "+5493514444444"},
    )
    assert valid_update.status_code == 200
    valid_data = valid_update.get_json()
    assert valid_data["ok"] is True
    assert valid_data["participant"]["email"] == "+5493514444444"

    resend_res = client.post(f"/api/sorteo/{draw_code}/resend", json={"mode": "console"})
    assert resend_res.status_code == 200
    resend_data = resend_res.get_json()
    assert resend_data["ok"] is True
    assert resend_data["channel"] == "whatsapp"
    assert "WhatsApp" in resend_data["message"]
