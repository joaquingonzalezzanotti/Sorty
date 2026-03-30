import importlib
import os


def test_kapso_template_button_uses_sub_type(monkeypatch, tmp_path):
    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'kapso_payload.db').as_posix()}"
    os.environ["EMAIL_MODE"] = "console"
    os.environ["WHATSAPP_MODE"] = "console"

    app_module = importlib.import_module("app")
    app_module = importlib.reload(app_module)

    captured = {}

    def fake_kapso_post_message(phone_number_id, api_key, payload):
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(app_module, "kapso_post_message", fake_kapso_post_message)

    app_module.kapso_send_template(
        phone_number_id="1116659434858231",
        api_key="test-key",
        to_contact="+5493518074317",
        template_name="amigo_invisible_resultados",
        language_code="es_AR",
        body_parameters=["ABC12345"],
        button_url_parameter="ABC12345",
        button_index="0",
    )

    components = captured["payload"]["template"]["components"]
    button_component = components[1]
    assert button_component["type"] == "button"
    assert button_component["sub_type"] == "url"
    assert "subtype" not in button_component
