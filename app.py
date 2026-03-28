import os
import json
import random
import re
import smtplib
import ssl
import threading
import urllib.error
import urllib.request
import uuid
import webbrowser
from email.message import EmailMessage
from email.utils import formataddr
from typing import Dict, List, Optional, Set, Tuple

from flask import Flask, jsonify, render_template, request, send_from_directory
from sqlalchemy.orm import joinedload

from models import Asignacion, Participante, Sorteo, db


def resolve_database_uri() -> str:
    """Pick the first available connection string, supporting Vercel + Neon defaults."""

    def normalize(uri: str) -> str:
        # Force psycopg (psycopg3) driver so we don't need psycopg2 in Vercel.
        if uri.startswith("postgres://"):
            return "postgresql+psycopg://" + uri[len("postgres://") :]
        if uri.startswith("postgresql://"):
            return uri.replace("postgresql://", "postgresql+psycopg://", 1)
        return uri

    candidates = [
        os.getenv("DATABASE_URL"),
        os.getenv("POSTGRES_PRISMA_URL"),  # Vercel + Neon (pooled)
        os.getenv("POSTGRES_URL"),  # Vercel + Neon (pooled)
        os.getenv("POSTGRES_URL_NON_POOLING"),
    ]
    for uri in candidates:
        if uri:
            return normalize(uri)
    return "sqlite:///sorty.db"


app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)
_schema_initialized = False


class AppError(Exception):
    """Errors that should be surfaced to the client with a friendly message."""


DRAW_CHANNELS = {"email", "whatsapp"}
WHATSAPP_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
DEFAULT_KAPSO_PHONE_NUMBER_ID = "1116659434858231"


def normalize_channel(value: Optional[str]) -> str:
    channel = (value or "email").strip().lower()
    if channel not in DRAW_CHANNELS:
        raise AppError("Canal invalido. Usa email o whatsapp.")
    return channel


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_whatsapp(value: str) -> str:
    raw = (value or "").strip()
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    compact = re.sub(r"[\s\-().]", "", raw)
    if not compact.startswith("+"):
        raise AppError("Numero invalido. Usa formato internacional E.164, por ejemplo +5491122334455.")

    normalized = "+" + re.sub(r"\D", "", compact[1:])
    if not WHATSAPP_E164_RE.match(normalized):
        raise AppError("Numero invalido. Usa formato internacional E.164, por ejemplo +5491122334455.")
    return normalized


def normalize_contact(value: str, channel: str, participant_name: Optional[str] = None) -> str:
    if channel == "email":
        email = normalize_email(value)
        if "@" not in email or "." not in email:
            target = f" para {participant_name}" if participant_name else ""
            raise AppError(f"Email invalido{target}.")
        return email

    phone = normalize_whatsapp(value)
    return phone


def infer_channel_from_contact(contact: str) -> str:
    return "email" if "@" in (contact or "") else "whatsapp"


def ensure_database_schema() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with app.app_context():
        db.create_all()
    _schema_initialized = True


def validate_participants(raw: List[dict], channel: str) -> List[dict]:
    participants: List[dict] = []
    seen_contacts: Set[str] = set()
    admin_count = 0

    for item in raw:
        name = (item.get("name") or "").strip()
        raw_contact = item.get("contact")
        if raw_contact is None:
            raw_contact = item.get("email")
        contact = normalize_contact(raw_contact or "", channel, participant_name=name)
        is_admin = bool(item.get("is_admin"))

        if not name:
            raise AppError("Cada participante necesita un nombre.")
        if contact in seen_contacts:
            if channel == "email":
                raise AppError(f"Email duplicado: {contact}")
            raise AppError(f"Numero duplicado: {contact}")

        seen_contacts.add(contact)
        admin_count += 1 if is_admin else 0
        participants.append({"name": name, "email": contact, "contact": contact, "is_admin": is_admin})

    if len(participants) < 3:
        raise AppError("Carga al menos tres participantes.")
    if admin_count != 1:
        raise AppError("Selecciona exactamente un Administrador.")

    return participants


def validate_exclusions(raw: List[dict], allowed_contacts: Set[str], channel: str) -> List[Tuple[str, str]]:
    exclusions: List[Tuple[str, str]] = []
    for item in raw:
        giver_raw = (item.get("from") or "").strip()
        receiver_raw = (item.get("to") or "").strip()
        giver = normalize_contact(giver_raw, channel) if giver_raw else ""
        receiver = normalize_contact(receiver_raw, channel) if receiver_raw else ""
        if not giver or not receiver:
            continue
        if giver not in allowed_contacts or receiver not in allowed_contacts:
            raise AppError("Las exclusiones deben referenciar participantes validos.")
        if giver == receiver:
            # No hace falta agregar la exclusion a si mismo: ya esta prohibido.
            continue
        exclusions.append((giver, receiver))
    return exclusions


def build_options(
    participants: List[dict], exclusions: List[Tuple[str, str]]
) -> Dict[str, List[str]]:
    emails = [p["email"] for p in participants]
    banned: Dict[str, Set[str]] = {email: {email} for email in emails}  # sin autoasignacion

    for giver, receiver in exclusions:
        banned.setdefault(giver, set()).add(receiver)

    options: Dict[str, List[str]] = {}
    for giver in emails:
        blocked = banned.get(giver, set())
        options[giver] = [email for email in emails if email not in blocked]
    return options


def find_assignments(
    participants: List[dict], exclusions: List[Tuple[str, str]]
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    options = build_options(participants, exclusions)
    emails = list(options.keys())

    impossible = [giver for giver, opts in options.items() if not opts]
    if impossible:
        return None, "Hay participantes sin receptores posibles por las restricciones."

    # Ordenamos por menor cantidad de opciones para acelerar el backtracking.
    order = sorted(emails, key=lambda g: len(options[g]))

    def backtrack(index: int, used: Set[str], mapping: Dict[str, str]) -> Optional[Dict[str, str]]:
        if index == len(order):
            return mapping

        giver = order[index]
        candidates = [r for r in options[giver] if r not in used]
        random.shuffle(candidates)

        for receiver in candidates:
            mapping[giver] = receiver
            used.add(receiver)
            result = backtrack(index + 1, used, mapping)
            if result:
                return result
            used.remove(receiver)
            mapping.pop(giver, None)
        return None

    for _ in range(400):
        assignment = backtrack(0, set(), {})
        if assignment:
            return assignment, None
        random.shuffle(order)

    return None, "No se pudo encontrar un sorteo valido con las restricciones dadas."


def resolve_logo_source(prefer_inline: bool = True) -> Tuple[str, Optional[dict]]:
    logo_small = "sorty_logo_small.png"
    logo_large = "sorty_logo.png"
    logo_path = os.path.join(app.static_folder, logo_small)

    if prefer_inline and os.path.isfile(logo_path):
        try:
            with open(logo_path, "rb") as handle:
                data = handle.read()
        except OSError:
            data = None
        if data:
            return "cid:sorty-logo", {
                "cid": "sorty-logo",
                "data": data,
                "maintype": "image",
                "subtype": "png",
            }

    filename = logo_small if os.path.isfile(logo_path) else logo_large
    base = (os.getenv("PUBLIC_APP_URL") or "").strip().rstrip("/")
    if not base:
        try:
            base = (request.url_root or "").rstrip("/")
        except RuntimeError:
            base = "http://localhost:5000"
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://") :]
    return f"{base}/static/{filename}", None


def attach_inline_logo(msg: EmailMessage, inline_logo: Optional[dict]) -> None:
    if not inline_logo:
        return
    html_part = msg.get_body(preferencelist=("html",))
    if not html_part:
        return
    html_part.add_related(
        inline_logo["data"],
        maintype=inline_logo["maintype"],
        subtype=inline_logo["subtype"],
        cid=inline_logo["cid"],
    )


def build_participant_email(
    giver: dict,
    receiver: dict,
    meta: dict,
    admin_contact: str,
    sender_name: str,
    logo_src: str,
) -> Tuple[str, str, str]:
    budget = meta.get("budget")
    deadline = meta.get("deadline")
    note = meta.get("note")

    subject_line = "Tu sorteo (Sorty)" + (f" - Entrega antes de {deadline}" if deadline else "")

    text_lines = [
        f"Hola {giver['name']},",
        "",
        f"Te toco regalar a: {receiver['name']}.",
    ]
    if budget:
        text_lines.append(f"Presupuesto sugerido: {budget}.")
    if deadline:
        text_lines.append(f"Fecha limite: {deadline}.")
    if note:
        text_lines.append("")
        text_lines.append(f"Mensaje: {note}")

    text_lines.append("")
    text_lines.append(f"Si necesitas algo, habla con {admin_contact}.")
    text_lines.append("")
    text_lines.append("Que sea una sorpresa linda! :)")

    text_body = "\n".join(text_lines)

    html_body = render_template(
        "emails/participant.html",
        giver=giver,
        receiver=receiver,
        budget=budget,
        deadline=deadline,
        note=note,
        admin_contact=admin_contact,
        sender_name=sender_name,
        logo_src=logo_src,
        max_width=520,
    )
    return subject_line, html_body, text_body


def build_admin_email(
    assignments: Dict[str, str],
    participants: List[dict],
    meta: dict,
    sender_name: str,
    logo_src: str,
    exclusions: List[Tuple[str, str]],
    admin_link: Optional[str] = None,
    code: Optional[str] = None,
) -> Tuple[str, str, str]:
    budget = meta.get("budget")
    deadline = meta.get("deadline")
    note = meta.get("note")

    by_email = {p["email"]: p for p in participants}
    rows_text = []
    for giver_email, receiver_email in assignments.items():
        giver = by_email[giver_email]
        receiver = by_email[receiver_email]
        rows_text.append(f"{giver['name']} -> {receiver['name']} ({receiver['email']})")

    subject = f"Resultados del Sorteo - {code}" if code else "Resultados del Sorteo - Administrador"
    rows = [
        {"giver_name": by_email[giver_email]["name"], "receiver_name": by_email[receiver_email]["name"]}
        for giver_email, receiver_email in assignments.items()
    ]
    exclusions_view = []
    for giver_email, receiver_email in exclusions:
        giver = by_email.get(giver_email)
        receiver = by_email.get(receiver_email)
        if giver and receiver:
            exclusions_view.append(
                {"giver_name": giver["name"], "receiver_name": receiver["name"]}
            )

    html_body = render_template(
        "emails/admin.html",
        rows=rows,
        exclusions=exclusions_view,
        budget=budget,
        deadline=deadline,
        note=note,
        admin_link=admin_link,
        sender_name=sender_name,
        logo_src=logo_src,
        code=code,
        max_width=560,
    )

    text_lines = ["Asignaciones completas Sorty:", ""]
    text_lines.extend(rows_text)
    if budget:
        text_lines.append(f"Presupuesto: {budget}")
    if deadline:
        text_lines.append(f"Fecha limite: {deadline}")
    if note:
        text_lines.append(f"Mensaje: {note}")
    if exclusions:
        text_lines.append("")
        text_lines.append("Exclusiones:")
        for giver_email, receiver_email in exclusions:
            giver = by_email.get(giver_email)
            receiver = by_email.get(receiver_email)
            if giver and receiver:
                text_lines.append(f"- {giver['name']} no regala a {receiver['name']}")

    if admin_link:
        text_lines.append("")
        text_lines.append(f"Ver sorteo: {admin_link}")

    text_body = "\n".join(text_lines)
    return subject, html_body, text_body


def phone_to_wa_id(phone_number: str) -> str:
    return re.sub(r"\D", "", phone_number)


def build_participant_whatsapp_text(
    giver: dict,
    receiver: dict,
    meta: dict,
    admin_contact: str,
) -> str:
    budget = meta.get("budget")
    deadline = meta.get("deadline")
    note = meta.get("note")

    lines = [
        f"Hola {giver['name']}!",
        f"Te toco regalar a: {receiver['name']}.",
    ]
    if budget:
        lines.append(f"Presupuesto sugerido: {budget}.")
    if deadline:
        lines.append(f"Fecha limite: {deadline}.")
    if note:
        lines.append(f"Mensaje grupal: {note}")
    lines.append(f"Si necesitas ayuda, contacta a {admin_contact}.")
    lines.append("Sorty")
    return "\n".join(lines)


def build_admin_whatsapp_text(
    admin_name: str,
    code: Optional[str],
    admin_link: Optional[str],
) -> str:
    lines = [
        f"Hola {admin_name}!",
        "Tu sorteo de Sorty ya fue generado.",
    ]
    if code:
        lines.append(f"Codigo: {code}")
    if admin_link:
        lines.append(f"Detalle y gestion: {admin_link}")
    lines.append("Sorty")
    return "\n".join(lines)


def whatsapp_templates_enabled() -> bool:
    value = (os.getenv("WHATSAPP_USE_TEMPLATES") or "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def template_value(value: Optional[str], default: str = "-") -> str:
    cleaned = (value or "").strip()
    return cleaned or default


def build_template_body_parameters(order: str, context: Dict[str, str]) -> List[str]:
    keys = [item.strip() for item in order.split(",") if item.strip()]
    return [template_value(context.get(key)) for key in keys]


def kapso_post_message(phone_number_id: str, api_key: str, payload: dict) -> dict:
    base_url = (os.getenv("KAPSO_BASE_URL") or "https://api.kapso.ai/meta/whatsapp/v24.0").rstrip("/")
    url = f"{base_url}/{phone_number_id}/messages"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise AppError(f"Kapso rechazo el envio ({exc.code}): {detail[:200]}") from exc
    except urllib.error.URLError as exc:
        raise AppError("No se pudo conectar con Kapso.") from exc


def kapso_send_text(phone_number_id: str, api_key: str, to_contact: str, body: str) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone_to_wa_id(to_contact),
        "type": "text",
        "text": {"body": body},
    }
    return kapso_post_message(phone_number_id=phone_number_id, api_key=api_key, payload=payload)


def kapso_send_template(
    phone_number_id: str,
    api_key: str,
    to_contact: str,
    template_name: str,
    language_code: str,
    body_parameters: List[str],
    button_url_parameter: Optional[str] = None,
    button_index: str = "0",
) -> dict:
    components = []
    if body_parameters:
        components.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": template_value(value)} for value in body_parameters],
            }
        )
    if button_url_parameter:
        components.append(
            {
                "type": "button",
                "subtype": "url",
                "index": button_index,
                "parameters": [{"type": "text", "text": template_value(button_url_parameter)}],
            }
        )

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone_to_wa_id(to_contact),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components,
        },
    }
    return kapso_post_message(phone_number_id=phone_number_id, api_key=api_key, payload=payload)


def dispatch_emails(
    participants: List[dict],
    assignments: Dict[str, str],
    meta: dict,
    exclusions: List[Tuple[str, str]],
    mode_override: Optional[str] = None,
    admin_link: Optional[str] = None,
) -> Dict[str, object]:
    mode = (mode_override or os.getenv("EMAIL_MODE", "smtp")).lower()
    sender_email = os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_USER") or "sorty@example.com"
    sender_name = os.getenv("SMTP_FROM_NAME") or "Sorty"

    admin = next(p for p in participants if p["is_admin"])
    admin_contact = admin["name"]
    by_email = {p["email"]: p for p in participants}
    logo_src, logo_inline = resolve_logo_source(prefer_inline=mode != "console")

    messages: List[EmailMessage] = []

    for giver in participants:
        receiver = by_email[assignments[giver["email"]]]
        subject, html_body, text_body = build_participant_email(
            giver, receiver, meta, admin_contact, sender_name, logo_src
        )
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((sender_name, sender_email))
        msg["To"] = formataddr((giver["name"], giver["email"]))
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        attach_inline_logo(msg, logo_inline)
        messages.append(msg)

    admin_subject, admin_html, admin_text = build_admin_email(
        assignments, participants, meta, sender_name, logo_src, exclusions, admin_link, code=meta.get("code")
    )
    admin_msg = EmailMessage()
    admin_msg["Subject"] = admin_subject
    admin_msg["From"] = formataddr((sender_name, sender_email))
    admin_msg["To"] = formataddr((admin["name"], admin["email"]))
    admin_msg.set_content(admin_text)
    admin_msg.add_alternative(admin_html, subtype="html")
    attach_inline_logo(admin_msg, logo_inline)
    messages.append(admin_msg)

    if mode == "console":
        for msg in messages:
            print("\n" + "-" * 60)
            print(f"To: {msg['To']}")
            print(f"Subject: {msg['Subject']}")
            plain = msg.get_body(preferencelist=("plain",))
            print(plain.get_content() if plain else msg.as_string())
        return {"mode": mode, "sent": False, "emails": len(messages)}

    if mode != "smtp":
        raise AppError(f"EMAIL_MODE desconocido: {mode}")

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")

    if not user or not password:
        raise AppError("Faltan credenciales SMTP: define SMTP_USER y SMTP_PASS.")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls(context=context)
        server.login(user, password)
        for msg in messages:
            server.send_message(msg)

        return {"mode": mode, "sent": True, "emails": len(messages)}


def dispatch_whatsapp_messages(
    participants: List[dict],
    assignments: Dict[str, str],
    meta: dict,
    exclusions: List[Tuple[str, str]],
    mode_override: Optional[str] = None,
    admin_link: Optional[str] = None,
) -> Dict[str, object]:
    mode = (mode_override or os.getenv("WHATSAPP_MODE", "kapso")).lower()
    admin = next(p for p in participants if p["is_admin"])
    by_email = {p["email"]: p for p in participants}

    outbound: List[Tuple[str, str, str]] = []
    for giver in participants:
        receiver = by_email[assignments[giver["email"]]]
        participant_text = build_participant_whatsapp_text(
            giver=giver,
            receiver=receiver,
            meta=meta,
            admin_contact=admin["name"],
        )
        outbound.append((giver["email"], giver["name"], participant_text))

    admin_text = build_admin_whatsapp_text(
        admin_name=admin["name"],
        code=meta.get("code"),
        admin_link=admin_link,
    )
    outbound.append((admin["email"], admin["name"], admin_text))

    if mode == "console":
        for to_contact, to_name, text in outbound:
            print("\n" + "-" * 60)
            print(f"To: {to_name} <{to_contact}>")
            print(text)
        return {"mode": mode, "sent": False, "emails": len(outbound)}

    if mode != "kapso":
        raise AppError("WHATSAPP_MODE desconocido: usa kapso o console.")

    api_key = (os.getenv("KAPSO_API_KEY") or "").strip()
    phone_number_id = (os.getenv("KAPSO_PHONE_NUMBER_ID") or DEFAULT_KAPSO_PHONE_NUMBER_ID).strip()
    if not api_key:
        raise AppError("Falta KAPSO_API_KEY para enviar por WhatsApp.")
    if not phone_number_id:
        raise AppError("Falta KAPSO_PHONE_NUMBER_ID para enviar por WhatsApp.")

    if whatsapp_templates_enabled():
        default_language = (os.getenv("KAPSO_TEMPLATE_LANGUAGE") or "es_MX").strip()
        participant_template_name = (os.getenv("KAPSO_TEMPLATE_PARTICIPANT_NAME") or "amigo_invisible_confirmacion").strip()
        admin_template_name = (os.getenv("KAPSO_TEMPLATE_ADMIN_NAME") or "amigo_invisible_results").strip()
        participant_language = (os.getenv("KAPSO_TEMPLATE_PARTICIPANT_LANGUAGE") or default_language).strip()
        admin_language = (os.getenv("KAPSO_TEMPLATE_ADMIN_LANGUAGE") or default_language).strip()
        participant_body_order = (
            os.getenv("KAPSO_TEMPLATE_PARTICIPANT_BODY_ORDER")
            or "receiver_name,budget,deadline,note,admin_name,code"
        ).strip()
        admin_body_order = (os.getenv("KAPSO_TEMPLATE_ADMIN_BODY_ORDER") or "code,budget,deadline,note").strip()
        admin_button_index = (os.getenv("KAPSO_TEMPLATE_ADMIN_BUTTON_INDEX") or "").strip()
        admin_button_value_source = (os.getenv("KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE_SOURCE") or "draw_link").strip().lower()

        if not participant_template_name:
            raise AppError("Falta KAPSO_TEMPLATE_PARTICIPANT_NAME.")
        if not admin_template_name:
            raise AppError("Falta KAPSO_TEMPLATE_ADMIN_NAME.")
        if not participant_language or not admin_language:
            raise AppError("Falta idioma de template. Define KAPSO_TEMPLATE_LANGUAGE (ej. es_MX).")

        shared_context = {
            "budget": template_value(meta.get("budget")),
            "deadline": template_value(meta.get("deadline")),
            "note": template_value(meta.get("note")),
            "code": template_value(meta.get("code")),
            "admin_name": template_value(admin.get("name")),
            "admin_contact": template_value(admin.get("email")),
            "draw_link": template_value(admin_link),
        }

        sent_count = 0
        for giver in participants:
            receiver = by_email[assignments[giver["email"]]]
            participant_context = {
                **shared_context,
                "giver_name": template_value(giver.get("name")),
                "receiver_name": template_value(receiver.get("name")),
                "receiver_contact": template_value(receiver.get("email")),
            }
            body_parameters = build_template_body_parameters(participant_body_order, participant_context)
            kapso_send_template(
                phone_number_id=phone_number_id,
                api_key=api_key,
                to_contact=giver["email"],
                template_name=participant_template_name,
                language_code=participant_language,
                body_parameters=body_parameters,
            )
            sent_count += 1

        admin_context = {
            **shared_context,
            "receiver_name": "-",
            "receiver_contact": "-",
            "giver_name": template_value(admin.get("name")),
        }
        admin_body_parameters = build_template_body_parameters(admin_body_order, admin_context)
        admin_button_parameter = None
        if admin_button_index:
            if admin_button_value_source == "code":
                admin_button_parameter = template_value(meta.get("code"))
            elif admin_button_value_source == "draw_link":
                admin_button_parameter = template_value(admin_link)
            else:
                admin_button_parameter = template_value(os.getenv("KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE"))

        kapso_send_template(
            phone_number_id=phone_number_id,
            api_key=api_key,
            to_contact=admin["email"],
            template_name=admin_template_name,
            language_code=admin_language,
            body_parameters=admin_body_parameters,
            button_url_parameter=admin_button_parameter,
            button_index=admin_button_index or "0",
        )
        sent_count += 1
        return {"mode": mode, "sent": True, "emails": sent_count, "transport": "kapso-template"}

    sent_count = 0
    for to_contact, _, text in outbound:
        kapso_send_text(phone_number_id=phone_number_id, api_key=api_key, to_contact=to_contact, body=text)
        sent_count += 1

    return {"mode": mode, "sent": True, "emails": sent_count, "transport": "kapso-text"}


def send_simple_email(
    to_email: str,
    to_name: str,
    subject: str,
    text_body: str,
    html_body: Optional[str] = None,
    mode_override: Optional[str] = None,
) -> Dict[str, object]:
    mode = (mode_override or os.getenv("EMAIL_MODE", "smtp")).lower()
    sender_email = os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_USER") or "sorty@example.com"
    sender_name = os.getenv("SMTP_FROM_NAME") or "Sorty"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = formataddr((to_name, to_email))
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    if mode == "console":
        print("\n" + "-" * 60)
        print(f"To: {msg['To']}")
        print(f"Subject: {msg['Subject']}")
        plain = msg.get_body(preferencelist=("plain",))
        print(plain.get_content() if plain else msg.as_string())
        return {"mode": mode, "sent": False, "emails": 1}

    if mode != "smtp":
        raise AppError(f"EMAIL_MODE desconocido: {mode}")

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")

    if not user or not password:
        raise AppError("Faltan credenciales SMTP: define SMTP_USER y SMTP_PASS.")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)

    return {"mode": mode, "sent": True, "emails": 1}


def send_simple_whatsapp(to_contact: str, text_body: str, mode_override: Optional[str] = None) -> Dict[str, object]:
    mode = (mode_override or os.getenv("WHATSAPP_MODE", "kapso")).lower()

    if mode == "console":
        print("\n" + "-" * 60)
        print(f"To: {to_contact}")
        print(text_body)
        return {"mode": mode, "sent": False, "emails": 1}

    if mode != "kapso":
        raise AppError("WHATSAPP_MODE desconocido: usa kapso o console.")

    api_key = (os.getenv("KAPSO_API_KEY") or "").strip()
    phone_number_id = (os.getenv("KAPSO_PHONE_NUMBER_ID") or DEFAULT_KAPSO_PHONE_NUMBER_ID).strip()
    if not api_key:
        raise AppError("Falta KAPSO_API_KEY para enviar por WhatsApp.")
    if not phone_number_id:
        raise AppError("Falta KAPSO_PHONE_NUMBER_ID para enviar por WhatsApp.")

    kapso_send_text(phone_number_id=phone_number_id, api_key=api_key, to_contact=to_contact, body=text_body)
    return {"mode": mode, "sent": True, "emails": 1}


def dispatch_notifications(
    participants: List[dict],
    assignments: Dict[str, str],
    meta: dict,
    exclusions: List[Tuple[str, str]],
    channel: str,
    mode_override: Optional[str] = None,
    admin_link: Optional[str] = None,
) -> Dict[str, object]:
    if channel == "whatsapp":
        return dispatch_whatsapp_messages(
            participants=participants,
            assignments=assignments,
            meta=meta,
            exclusions=exclusions,
            mode_override=mode_override,
            admin_link=admin_link,
        )

    return dispatch_emails(
        participants=participants,
        assignments=assignments,
        meta=meta,
        exclusions=exclusions,
        mode_override=mode_override,
        admin_link=admin_link,
    )


def build_sorteo_link(sorteo: Sorteo) -> str:
    """Return an absolute link to the stored draw (sorteo)."""
    base = (os.getenv("PUBLIC_APP_URL") or "").strip().rstrip("/")
    if not base:
        try:
            base = (request.url_root or "").rstrip("/")
        except RuntimeError:
            base = "http://localhost:5000"
    return f"{base}/sorteo/{sorteo.code}"


def load_draw_data(code: str) -> Tuple[Sorteo, dict]:
    """Fetch draw data (participants, exclusions, assignments) by code or UUID."""
    ensure_database_schema()
    query = Sorteo.query.options(
        joinedload(Sorteo.participantes).joinedload(Participante.exclusiones),
        joinedload(Sorteo.asignaciones).joinedload(Asignacion.giver),
        joinedload(Sorteo.asignaciones).joinedload(Asignacion.receiver),
    )

    sorteo = query.filter(Sorteo.code == code).first()
    if not sorteo:
        try:
            sorteo = query.filter(Sorteo.public_id == uuid.UUID(code)).first()
        except ValueError:
            sorteo = None
    if not sorteo:
        raise AppError("Sorteo no encontrado.")

    channel = infer_channel_from_contact(sorteo.email_admin)
    participants = []
    for p in sorteo.participantes:
        participants.append(
            {
                "id": p.id,
                "name": p.nombre,
                "email": p.email,
                "contact": p.email,
                "is_admin": p.email.lower() == sorteo.email_admin.lower(),
            }
        )

    exclusions: List[Tuple[str, str]] = []
    for p in sorteo.participantes:
        for excl in p.exclusiones:
            exclusions.append((p.email, excl.email))

    assignments: Dict[str, str] = {}
    for a in sorteo.asignaciones:
        assignments[a.giver.email] = a.receiver.email

    payload = {
        "id": str(sorteo.public_id),
        "code": sorteo.code,
        "name": sorteo.nombre,
        "channel": channel,
        "email_admin": sorteo.email_admin,
        "admin_contact": sorteo.email_admin,
        "participants": participants,
        "exclusions": exclusions,
        "assignments": [
            {
                "giver_email": giver,
                "receiver_email": receiver,
                "giver_contact": giver,
                "receiver_contact": receiver,
                "giver_name": next((p["name"] for p in participants if p["email"] == giver), giver),
                "receiver_name": next((p["name"] for p in participants if p["email"] == receiver), receiver),
            }
            for giver, receiver in assignments.items()
        ],
    }
    return sorteo, payload


def update_participant_contact(sorteo: Sorteo, participant_id: int, new_contact: str, notify_previous: bool) -> dict:
    draw_channel = infer_channel_from_contact(sorteo.email_admin)
    new_contact_norm = normalize_contact(new_contact, draw_channel)

    participant = next((p for p in sorteo.participantes if p.id == participant_id), None)
    if not participant:
        raise AppError("Participante no encontrado en este sorteo.")

    if any(p.email.lower() == new_contact_norm.lower() and p.id != participant.id for p in sorteo.participantes):
        if draw_channel == "email":
            raise AppError("Ya existe un participante con ese email.")
        raise AppError("Ya existe un participante con ese numero.")

    old_contact = participant.email
    participant.email = new_contact_norm

    admin_contact_updated = False
    if sorteo.email_admin.lower() == old_contact.lower():
        sorteo.email_admin = new_contact_norm
        admin_contact_updated = True

    db.session.commit()

    notified = False
    if notify_previous and old_contact.lower() != new_contact_norm.lower() and draw_channel == "email":
        admin_contact = f"{sorteo.nombre} ({sorteo.email_admin})"
        subject = "Correccion de correo - Sorty"
        text_body = (
            f"Hola,\n\n"
            f"Se registró una corrección de correo para el sorteo '{sorteo.nombre}'.\n"
            f"Por favor, desestima el correo anterior enviado a esta dirección.\n"
            f"Si necesitas ayuda, contacta a {admin_contact}.\n"
        )
        html_body = (
            "<!doctype html><html><body style=\"font-family:'Segoe UI', Arial, sans-serif; background:#f6f8fb; padding:24px; color:#1d2433;\">"
            f"<div style='max-width:520px; margin:0 auto; background:#ffffff; border-radius:12px; padding:20px; box-shadow:0 10px 30px rgba(0,0,0,0.08);'>"
            f"<h2 style='margin:0 0 12px; font-size:22px; color:#111827;'>Correccion de correo</h2>"
            f"<p style='margin:8px 0; font-size:15px;'>El Administrador ajustó la dirección del sorteo <strong>{sorteo.nombre}</strong>.</p>"
            f"<p style='margin:8px 0; font-size:15px;'>Por favor desestima el correo anterior enviado a esta dirección.</p>"
            f"<p style='margin:14px 0; font-size:14px; color:#4b5563;'>Si necesitas algo, contacta a {admin_contact}.</p>"
            f"</div></body></html>"
        )
        send_simple_email(old_contact, participant.nombre, subject, text_body, html_body)
        notified = True

    return {
        "id": participant.id,
        "name": participant.nombre,
        "email": participant.email,
        "contact": participant.email,
        "is_admin": participant.email.lower() == sorteo.email_admin.lower(),
        "notified_previous": notified,
        "admin_email_updated": admin_contact_updated,
        "admin_contact_updated": admin_contact_updated,
    }


def save_draw_to_db(
    participants: List[dict],
    exclusions: List[Tuple[str, str]],
    assignments: Dict[str, str],
    meta: dict,
) -> Sorteo:
    """Persist the draw, participants, exclusions and assignments."""
    admin = meta.get("admin") or next(p for p in participants if p["is_admin"])
    name = (meta.get("name") or "").strip() or f"Sorteo de {admin['name']}"

    sorteo = Sorteo(nombre=name, email_admin=admin["email"], estado="finalizado")
    db.session.add(sorteo)

    by_email: Dict[str, Participante] = {}
    for p in participants:
        record = Participante(sorteo=sorteo, nombre=p["name"], email=p["email"])
        db.session.add(record)
        by_email[p["email"]] = record

    for giver_email, receiver_email in exclusions:
        giver = by_email.get(giver_email)
        receiver = by_email.get(receiver_email)
        if giver and receiver:
            giver.exclusiones.append(receiver)

    for giver_email, receiver_email in assignments.items():
        giver = by_email[giver_email]
        receiver = by_email[receiver_email]
        db.session.add(Asignacion(sorteo=sorteo, giver=giver, receiver=receiver))

    db.session.commit()
    return sorteo


def assignment_for_client(assignments: Dict[str, str], participants: List[dict]) -> List[dict]:
    by_email = {p["email"]: p for p in participants}
    rendered = []
    for giver_email, receiver_email in assignments.items():
        giver = by_email[giver_email]
        receiver = by_email[receiver_email]
        rendered.append(
            {
                "giver_name": giver["name"],
                "giver_email": giver["email"],
                "giver_contact": giver["email"],
                "receiver_name": receiver["name"],
                "receiver_email": receiver["email"],
                "receiver_contact": receiver["email"],
            }
        )
    return rendered


@app.route("/", methods=["GET"])
def landing():
    base = (os.getenv("PUBLIC_APP_URL") or request.url_root or "").strip().rstrip("/")
    if not base:
        base = "https://sorty-neon.vercel.app"
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://") :]
    return render_template(
        "landing.html",
        canonical_url=f"{base}/",
        app_url=f"{base}/app",
        og_image_url=f"{base}/static/sorty_logo.png",
    )


@app.route("/app", methods=["GET"])
def index():
    email_mode = os.getenv("EMAIL_MODE", "smtp").lower()
    whatsapp_mode = os.getenv("WHATSAPP_MODE", "kapso").lower()
    return render_template("index.html", email_mode=email_mode, whatsapp_mode=whatsapp_mode)


@app.route("/preview/email/participant", methods=["GET"])
def preview_participant_email():
    giver = {"name": "Carla", "email": "carla@example.com"}
    receiver = {"name": "Pablo", "email": "pablo@example.com"}
    meta = {
        "budget": "20 USD",
        "deadline": "20/12",
        "note": "Traer regalo envuelto.",
    }
    logo_src, _ = resolve_logo_source(prefer_inline=False)
    _, html_body, _ = build_participant_email(
        giver,
        receiver,
        meta,
        admin_contact="Nora (admin)",
        sender_name="Sorty",
        logo_src=logo_src,
    )
    return html_body


@app.route("/preview/email/admin", methods=["GET"])
def preview_admin_email():
    participants = [
        {"name": "Nora", "email": "nora@example.com", "is_admin": True},
        {"name": "Carla", "email": "carla@example.com", "is_admin": False},
        {"name": "Pablo", "email": "pablo@example.com", "is_admin": False},
    ]
    assignments = {
        "nora@example.com": "carla@example.com",
        "carla@example.com": "pablo@example.com",
        "pablo@example.com": "nora@example.com",
    }
    exclusions = [("carla@example.com", "nora@example.com")]
    meta = {
        "budget": "20 USD",
        "deadline": "20/12",
        "note": "Intercambio presencial.",
    }
    logo_src, _ = resolve_logo_source(prefer_inline=False)
    _, html_body, _ = build_admin_email(
        assignments,
        participants,
        meta,
        sender_name="Sorty",
        logo_src=logo_src,
        exclusions=exclusions,
        admin_link="https://sorty.example/sorteo/ABC123",
        code="ABC123",
    )
    return html_body


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "sorty_logo.png", mimetype="image/png")


@app.route("/sorteo/<code>", methods=["GET"])
@app.route("/draw/<code>", methods=["GET"])  # alias antiguo
def sorteo_view(code: str):
    try:
        sorteo, data = load_draw_data(code)
    except AppError as exc:
        return render_template("sorteo.html", error=str(exc), data=None), 404
    except Exception:
        return render_template("sorteo.html", error="Error interno al cargar el sorteo.", data=None), 500

    return render_template("sorteo.html", data=data, draw_link=build_sorteo_link(sorteo))


@app.route("/api/sorteo", methods=["POST"])
@app.route("/api/draw", methods=["POST"])  # alias antiguo
def api_draw():
    payload = request.get_json(force=True) or {}
    send_notifications = bool(payload.get("send"))
    meta = payload.get("meta") or {}
    mode_override = (payload.get("mode") or "").strip().lower() or None
    channel = normalize_channel(payload.get("channel"))

    try:
        ensure_database_schema()
        participants = validate_participants(payload.get("participants") or [], channel)
        exclusions = validate_exclusions(payload.get("exclusions") or [], {p["email"] for p in participants}, channel)
        assignments, err = find_assignments(participants, exclusions)
        if err or not assignments:
            raise AppError(err or "No se pudo generar un sorteo valido.")

        if mode_override:
            allowed_modes = {"console", "smtp"} if channel == "email" else {"console", "kapso"}
            if mode_override not in allowed_modes:
                if channel == "email":
                    raise AppError("Modo de email invalido. Usa console o smtp.")
                raise AppError("Modo de WhatsApp invalido. Usa console o kapso.")

        admin = next(p for p in participants if p["is_admin"])
        meta_clean = {
            "budget": (meta.get("budget") or "").strip(),
            "deadline": (meta.get("deadline") or "").strip(),
            "note": (meta.get("note") or "").strip(),
            "admin": admin,
        }

        sorteo_record = None
        draw_link = None
        try:
            sorteo_record = save_draw_to_db(participants, exclusions, assignments, meta_clean)
            draw_link = build_sorteo_link(sorteo_record)
            meta_clean["code"] = sorteo_record.code
        except Exception:
            db.session.rollback()
            raise

        delivery_status = None
        if send_notifications:
            delivery_status = dispatch_notifications(
                participants=participants,
                assignments=assignments,
                meta=meta_clean,
                exclusions=exclusions,
                channel=channel,
                mode_override=mode_override,
                admin_link=draw_link,
            )

        default_mode = os.getenv("EMAIL_MODE", "smtp") if channel == "email" else os.getenv("WHATSAPP_MODE", "kapso")
        response = {
            "ok": True,
            "message": (
                "Correos enviados."
                if send_notifications and channel == "email"
                else "Mensajes de WhatsApp enviados."
                if send_notifications
                else "Simulacion lista. No se enviaron mensajes."
            ),
            "assignment": assignment_for_client(assignments, participants),
            "email_status": delivery_status,
            "delivery_status": delivery_status,
            "channel": channel,
            "mode": mode_override or default_mode,
            "draw_id": str(sorteo_record.public_id) if sorteo_record else None,
            "draw_code": sorteo_record.code if sorteo_record else None,
            "draw_link": draw_link,
            "sorteo_link": draw_link,
        }
        return jsonify(response)

    except AppError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - safety net
        return jsonify({"ok": False, "error": "Error inesperado en el servidor."}), 500


@app.route("/api/sorteo/<code>", methods=["GET"])
@app.route("/api/draw/<code>", methods=["GET"])  # alias antiguo
def api_draw_get(code: str):
    try:
        _, data = load_draw_data(code)
        return jsonify({"ok": True, "draw": data})
    except AppError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception:
        return jsonify({"ok": False, "error": "Error inesperado en el servidor."}), 500


@app.route("/api/sorteo/<code>/resend", methods=["POST"])
@app.route("/api/draw/<code>/resend", methods=["POST"])  # alias antiguo
def api_draw_resend(code: str):
    payload = request.get_json(silent=True) or {}
    mode_override = (payload.get("mode") or "").strip().lower() or None
    try:
        sorteo, data = load_draw_data(code)
        channel = normalize_channel(data.get("channel") or infer_channel_from_contact(data.get("email_admin") or ""))
        participants = data["participants"]
        exclusions = data["exclusions"]
        assignments = {item["giver_email"]: item["receiver_email"] for item in data["assignments"]}

        admin = next((p for p in participants if p["is_admin"]), None)
        if not admin:
            raise AppError("No se encontro Administrador para este sorteo.")

        meta_clean = {
            "budget": "",
            "deadline": "",
            "note": "",
            "admin": admin,
            "code": data.get("code"),
        }
        delivery_status = dispatch_notifications(
            participants=participants,
            assignments=assignments,
            meta=meta_clean,
            exclusions=exclusions,
            channel=channel,
            mode_override=mode_override,
            admin_link=build_sorteo_link(sorteo),
        )
        message = "Correos reenviados." if channel == "email" else "Mensajes de WhatsApp reenviados."
        return jsonify(
            {
                "ok": True,
                "message": message,
                "email_status": delivery_status,
                "delivery_status": delivery_status,
                "channel": channel,
            }
        )
    except AppError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Error inesperado en el servidor."}), 500


@app.route("/api/sorteo/<code>/participant/<int:participant_id>/email", methods=["PATCH"])
@app.route("/api/draw/<code>/participant/<int:participant_id>/email", methods=["PATCH"])  # alias antiguo
@app.route("/api/sorteo/<code>/participant/<int:participant_id>/contact", methods=["PATCH"])
@app.route("/api/draw/<code>/participant/<int:participant_id>/contact", methods=["PATCH"])  # alias antiguo
def api_draw_update_contact(code: str, participant_id: int):
    payload = request.get_json(force=True) or {}
    new_contact = (payload.get("contact") or payload.get("email") or "").strip()
    notify_previous = bool(payload.get("notify_previous"))
    try:
        sorteo, _ = load_draw_data(code)
        updated = update_participant_contact(sorteo, participant_id, new_contact, notify_previous)
        return jsonify({"ok": True, "participant": updated})
    except AppError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Error inesperado en el servidor."}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    url = f"http://localhost:{port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port, debug=True)
