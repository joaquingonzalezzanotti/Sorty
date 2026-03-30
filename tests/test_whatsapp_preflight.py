import importlib

import pytest


def _load_app_module(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'wa_preflight.db').as_posix()}")
    monkeypatch.setenv("EMAIL_MODE", "console")
    monkeypatch.setenv("WHATSAPP_MODE", "kapso")
    monkeypatch.setenv("WHATSAPP_USE_TEMPLATES", "1")
    monkeypatch.setenv("KAPSO_API_KEY", "test-key")
    monkeypatch.setenv("KAPSO_PHONE_NUMBER_ID", "1116659434858231")
    monkeypatch.setenv("KAPSO_TEMPLATE_PARTICIPANT_NAME", "amigo_invisible_confirmacion")
    monkeypatch.setenv("KAPSO_TEMPLATE_ADMIN_NAME", "amigo_invisible_resultados")
    monkeypatch.setenv("KAPSO_TEMPLATE_PARTICIPANT_LANGUAGE", "es_AR")
    monkeypatch.setenv("KAPSO_TEMPLATE_ADMIN_LANGUAGE", "es_AR")
    monkeypatch.setenv("KAPSO_TEMPLATE_PARTICIPANT_BODY_ORDER", "giver_name,receiver_name,budget,deadline,note,admin_name")
    monkeypatch.setenv("KAPSO_TEMPLATE_ADMIN_BODY_ORDER", "code")
    monkeypatch.setenv("KAPSO_TEMPLATE_ADMIN_BUTTON_INDEX", "0")
    monkeypatch.setenv("KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE_SOURCE", "code")

    module = importlib.import_module("app")
    module = importlib.reload(module)
    return module


def _sample_draw_payload():
    participants = [
        {"name": "Admin", "email": "+5493518000001", "is_admin": True},
        {"name": "Ana", "email": "+5493518000002", "is_admin": False},
        {"name": "Beto", "email": "+5493518000003", "is_admin": False},
    ]
    assignments = {
        "+5493518000001": "+5493518000002",
        "+5493518000002": "+5493518000003",
        "+5493518000003": "+5493518000001",
    }
    meta = {"budget": "$200", "deadline": "24/04", "note": "Entregamos en la cena", "code": "ABC12345"}
    return participants, assignments, meta


def test_whatsapp_templates_send_admin_first(monkeypatch, tmp_path):
    app_module = _load_app_module(monkeypatch, tmp_path)
    participants, assignments, meta = _sample_draw_payload()

    calls = []

    def fake_kapso_send_template(**kwargs):
        calls.append(kwargs)
        if kwargs["to_contact"] == "+5493518000001":
            raise app_module.AppError(
                "Falla simulada admin.",
                code="KAPSO_100",
                hint="param error",
                source="kapso",
            )
        return {"ok": True}

    monkeypatch.setattr(app_module, "kapso_send_template", fake_kapso_send_template)

    with pytest.raises(app_module.AppError) as raised:
        app_module.dispatch_whatsapp_messages(
            participants=participants,
            assignments=assignments,
            meta=meta,
            exclusions=[],
            mode_override="kapso",
            admin_link="https://sorty.com.ar/sorteo/ABC12345",
        )

    assert "Administrador" in raised.value.message
    assert len(calls) == 1
    assert calls[0]["to_contact"] == "+5493518000001"


def test_whatsapp_templates_preflight_rejects_invalid_body_order(monkeypatch, tmp_path):
    app_module = _load_app_module(monkeypatch, tmp_path)
    participants, assignments, meta = _sample_draw_payload()
    monkeypatch.setenv("KAPSO_TEMPLATE_PARTICIPANT_BODY_ORDER", "giver_name,clave_invalida")

    calls = []

    def fake_kapso_send_template(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(app_module, "kapso_send_template", fake_kapso_send_template)

    with pytest.raises(app_module.AppError) as raised:
        app_module.dispatch_whatsapp_messages(
            participants=participants,
            assignments=assignments,
            meta=meta,
            exclusions=[],
            mode_override="kapso",
            admin_link="https://sorty.com.ar/sorteo/ABC12345",
        )

    assert "KAPSO_TEMPLATE_PARTICIPANT_BODY_ORDER" in raised.value.message
    assert len(calls) == 0
