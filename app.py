import imaplib
import hashlib
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
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import formataddr
from typing import Dict, List, Optional, Set, Tuple
from xml.sax.saxutils import escape as xml_escape

from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory
from sqlalchemy import inspect, text
from sqlalchemy.orm import joinedload

from models import Asignacion, EmailEnvio, Participante, Sorteo, db


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
    vercel_env = (os.getenv("VERCEL_ENV") or "").strip().lower()
    if vercel_env == "production":
        raise RuntimeError(
            "Falta DATABASE_URL/POSTGRES_* en production. "
            "Configura Postgres en Vercel para evitar fallback a SQLite."
        )
    return "sqlite:///sorty.db"


app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)
_schema_initialized = False


def resolve_public_base_url() -> str:
    primary_domain = (os.getenv("PRIMARY_DOMAIN") or "sorty.com.ar").strip().lower()
    vercel_env = (os.getenv("VERCEL_ENV") or "").strip().lower()
    base = (os.getenv("PUBLIC_APP_URL") or "").strip().rstrip("/")
    if base and vercel_env == "production" and "vercel.app" in base:
        base = f"https://{primary_domain}"
    if not base:
        host = ""
        try:
            host = ((request.host or "").split(":")[0]).strip().lower()
        except RuntimeError:
            host = ""
        if host in {"localhost", "127.0.0.1"} or host.endswith(".localhost"):
            try:
                base = (request.url_root or "").strip().rstrip("/")
            except RuntimeError:
                base = f"https://{primary_domain}"
        else:
            base = f"https://{primary_domain}"
    if not base:
        base = "https://sorty.com.ar"
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://") :]
    return base


@app.before_request
def enforce_primary_domain():
    vercel_env = (os.getenv("VERCEL_ENV") or "").strip().lower()
    if vercel_env == "preview":
        return None

    primary_domain = (os.getenv("PRIMARY_DOMAIN") or "sorty.com.ar").strip().lower()
    if not primary_domain:
        return None

    host = ((request.host or "").split(":")[0]).strip().lower()
    if host in {"localhost", "127.0.0.1"} or host.endswith(".localhost"):
        return None
    if host == primary_domain:
        return None

    query = request.query_string.decode("utf-8", errors="ignore")
    destination = f"https://{primary_domain}{request.path}"
    if query:
        destination = f"{destination}?{query}"
    return redirect(destination, code=308)


@app.after_request
def add_security_headers(response):
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers["Content-Security-Policy"] = csp
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


class AppError(Exception):
    """Errors that should be surfaced to the client with a friendly message."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        hint: Optional[str] = None,
        source: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
        self.source = source

    def to_payload(self) -> dict:
        payload = {"ok": False, "error": self.message}
        if self.code:
            payload["error_code"] = self.code
        if self.hint:
            payload["error_hint"] = self.hint
        if self.source:
            payload["error_source"] = self.source
        return payload


def error_payload_from_exception(exc: Exception, default_message: str = "Error inesperado en el servidor.") -> dict:
    if isinstance(exc, AppError):
        return exc.to_payload()
    return {"ok": False, "error": default_message}


def log_api_app_error(endpoint: str, payload: dict, exc: AppError) -> None:
    safe_payload = {
        "channel": payload.get("channel"),
        "mode": payload.get("mode"),
        "send": bool(payload.get("send")),
        "participants_count": len(payload.get("participants") or []),
        "exclusions_count": len(payload.get("exclusions") or []),
    }
    diagnostic = {
        "endpoint": endpoint,
        "error": exc.message,
        "error_code": exc.code,
        "error_hint": exc.hint,
        "error_source": exc.source,
        "request": safe_payload,
    }
    app.logger.warning("API_APP_ERROR %s", json.dumps(diagnostic, ensure_ascii=False))


def parse_kapso_error_detail(detail: str) -> dict:
    raw_detail = (detail or "").strip()
    parsed: dict = {}
    try:
        parsed = json.loads(raw_detail or "{}")
    except Exception:
        parsed = {}

    root = parsed if isinstance(parsed, dict) else {}
    err = root.get("error") if isinstance(root.get("error"), dict) else root

    def from_hash_text_text(field: str) -> str:
        # Some providers proxy Meta errors as Ruby-hash-like strings: "key" => "value".
        match = re.search(rf'"{re.escape(field)}"\s*(?::|=>)\s*"([^"]*)"', raw_detail)
        return match.group(1).strip() if match else ""

    def from_hash_text_int(field: str):
        match = re.search(rf'"{re.escape(field)}"\s*(?::|=>)\s*(\d+)', raw_detail)
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return match.group(1)

    message = str(err.get("message") or "").strip() or from_hash_text_text("message")
    user_msg = str(err.get("error_user_msg") or "").strip() or from_hash_text_text("error_user_msg")
    user_title = str(err.get("error_user_title") or "").strip() or from_hash_text_text("error_user_title")
    error_type = str(err.get("type") or "").strip() or from_hash_text_text("type")
    fbtrace_id = str(err.get("fbtrace_id") or "").strip() or from_hash_text_text("fbtrace_id")
    code = err.get("code")
    subcode = err.get("error_subcode")
    if code is None:
        code = from_hash_text_int("code")
    if subcode is None:
        subcode = from_hash_text_int("error_subcode")

    if code is None:
        code_match = re.search(r"\bcode\b[^0-9]{0,8}(\d{3,6})\b", raw_detail, flags=re.IGNORECASE)
        if code_match:
            try:
                code = int(code_match.group(1))
            except Exception:
                code = code_match.group(1)

    return {
        "message": message or raw_detail[:240],
        "type": error_type,
        "code": code,
        "subcode": subcode,
        "user_title": user_title,
        "user_msg": user_msg,
        "fbtrace_id": fbtrace_id,
        "raw": raw_detail[:500],
    }


def kapso_error_hint(status_code: int, info: dict) -> str:
    raw = " ".join(
        [
            str(info.get("message") or ""),
            str(info.get("user_msg") or ""),
            str(info.get("type") or ""),
            str(info.get("raw") or ""),
        ]
    ).lower()
    code = str(info.get("code") or "")

    if code == "1010":
        if raw.strip() == "error code: 1010":
            return (
                "El gateway rechazo la solicitud (WAF/Cloudflare 1010). "
                "Suele pasar por User-Agent/headers. Usa User-Agent explicito en requests a Kapso."
            )
        return (
            "Revisa template/idioma y cantidad de parametros. "
            "Tambien valida que KAPSO_API_KEY y KAPSO_PHONE_NUMBER_ID pertenezcan al mismo proyecto LIVE."
        )
    if code == "100" or "invalid parameter" in raw or "param" in raw:
        return "Hay un parametro invalido. Revisa nombre del template, idioma y orden/cantidad de variables."
    if "template" in raw and ("not found" in raw or "does not exist" in raw):
        return "El template no existe para ese numero o workspace. Verifica nombre exacto e idioma aprobado."
    if "language" in raw:
        return "El idioma del template no coincide con uno aprobado. Usa el locale exacto (ej: es_AR)."
    if "quality" in raw or "limit" in raw or "rate" in raw:
        return "El numero puede estar limitado por calidad o tier. Revisa Messaging limits y estado del display name."
    if "permission" in raw or "unauthorized" in raw:
        return "Verifica credenciales, permisos del numero y estado del Business/WhatsApp account."
    if status_code in {401, 403}:
        return "HTTP 401/403 desde Kapso. Revisa API key, phone_number_id y permisos del numero en el workspace."
    return "Revisa logs de Kapso/Meta para el request fallido (template, idioma, parametros y numero destino)."


def build_kapso_app_error(status_code: int, detail: str, payload: dict) -> AppError:
    info = parse_kapso_error_detail(detail)
    code = info.get("code")
    subcode = info.get("subcode")
    reason = info.get("user_msg") or info.get("message") or (detail or "").strip()[:200] or "Error desconocido."

    context = []
    if payload.get("type") == "template":
        template = payload.get("template") or {}
        template_name = template.get("name")
        language_code = (template.get("language") or {}).get("code")
        if template_name:
            context.append(f"template={template_name}")
        if language_code:
            context.append(f"lang={language_code}")
    to_contact = payload.get("to")
    if to_contact:
        context.append(f"to={to_contact}")

    code_text = f", code {code}" if code is not None else ""
    subcode_text = f", subcode {subcode}" if subcode is not None else ""
    context_text = f" ({', '.join(context)})" if context else ""

    message = f"Kapso rechazo el envio de WhatsApp (HTTP {status_code}{code_text}{subcode_text}). {reason}{context_text}"
    hint = kapso_error_hint(status_code, info)
    error_code = f"KAPSO_{code}" if code is not None else f"KAPSO_HTTP_{status_code}"
    return AppError(message, code=error_code, hint=hint, source="kapso")


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


def apply_schema_compatibility_migrations() -> None:
    inspector = inspect(db.engine)
    if "sorteo" not in set(inspector.get_table_names()):
        return

    sorteo_columns = {column.get("name") for column in inspector.get_columns("sorteo")}
    if "admin_contact" not in sorteo_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE sorteo ADD COLUMN admin_contact VARCHAR(320)"))
            if "email_admin" in sorteo_columns:
                connection.execute(
                    text(
                        "UPDATE sorteo "
                        "SET admin_contact = email_admin "
                        "WHERE (admin_contact IS NULL OR admin_contact = '') "
                        "AND email_admin IS NOT NULL"
                    )
                )

        app.logger.warning(
            "DB_SCHEMA_COMPAT added missing sorteo.admin_contact column from legacy schema."
        )
        # Refresh column list after ALTER TABLE so fallback copy below has up-to-date metadata.
        inspector = inspect(db.engine)
        sorteo_columns = {column.get("name") for column in inspector.get_columns("sorteo")}

    if "admin_contact" in sorteo_columns and "email_admin" in sorteo_columns:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE sorteo "
                    "SET admin_contact = email_admin "
                    "WHERE (admin_contact IS NULL OR admin_contact = '') "
                    "AND email_admin IS NOT NULL"
                )
            )


def ensure_database_schema() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with app.app_context():
        db.create_all()
        apply_schema_compatibility_migrations()
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
def ensure_email_tracking_table() -> bool:
    try:
        inspector = inspect(db.engine)
        if "email_envio" not in inspector.get_table_names():
            EmailEnvio.__table__.create(db.engine)
        return True
    except Exception:
        return False


def format_smtp_error(code: object, response: object) -> str:
    if isinstance(response, bytes):
        message = response.decode(errors="replace")
    else:
        message = str(response or "")
    if code:
        return f"{code} {message}".strip()
    return message.strip() or "Destinatario rechazado."


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
    base = resolve_public_base_url()
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


def imap_configured() -> bool:
    return bool(os.getenv("IMAP_HOST") and os.getenv("IMAP_USER") and os.getenv("IMAP_PASS"))


def extract_emails(text: str) -> Set[str]:
    return {match.lower() for match in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)}


def is_bounce_message(msg: EmailMessage) -> bool:
    subject = (msg.get("Subject") or "").lower()
    sender = (msg.get("From") or "").lower()
    if "mailer-daemon" in sender or "mail delivery subsystem" in sender:
        return True
    return "delivery status notification" in subject or "undelivered" in subject or "address not found" in subject


def extract_bounce_data(msg: EmailMessage) -> Tuple[Set[str], Optional[str]]:
    recipients: Set[str] = set()
    diagnostic = None

    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type == "message/delivery-status":
            payload = part.get_payload()
            blocks = payload if isinstance(payload, list) else []
            for block in blocks:
                for key in ("Final-Recipient", "Original-Recipient"):
                    value = block.get(key)
                    if value:
                        recipients.update(extract_emails(value))
                if not diagnostic:
                    diag = block.get("Diagnostic-Code")
                    if diag:
                        diagnostic = str(diag)
        elif content_type == "text/plain":
            try:
                charset = part.get_content_charset() or "utf-8"
                text_bytes = part.get_payload(decode=True)
                if text_bytes:
                    text = text_bytes.decode(charset, errors="replace")
                else:
                    text = part.get_payload()
                if text:
                    recipients.update(extract_emails(text))
                    if not diagnostic:
                        for line in text.splitlines():
                            if "diagnostic-code" in line.lower():
                                diagnostic = line.split(":", 1)[-1].strip()
                                break
            except Exception:
                continue

    if not recipients:
        for header in ("Final-Recipient", "Original-Recipient", "To"):
            value = msg.get(header)
            if value:
                recipients.update(extract_emails(value))

    if not diagnostic:
        subject = msg.get("Subject")
        if subject:
            diagnostic = subject.strip()

    if diagnostic:
        diagnostic = diagnostic.strip()
        if len(diagnostic) > 255:
            diagnostic = diagnostic[:252] + "..."

    return recipients, diagnostic


def poll_bounces_for_draw(sorteo_id: int) -> dict:
    if not ensure_email_tracking_table():
        return {"ok": False, "error": "No se pudo preparar el tracking de emails."}
    if not imap_configured():
        return {"ok": False, "error": "IMAP no configurado."}

    sorteo = Sorteo.query.get(sorteo_id)
    if not sorteo:
        return {"ok": False, "error": "Sorteo no encontrado."}

    participant_map = {normalize_email(p.email): p.id for p in sorteo.participantes}
    if not participant_map:
        return {"ok": True, "updated": 0}

    host = os.getenv("IMAP_HOST")
    port = int(os.getenv("IMAP_PORT", "993"))
    user = os.getenv("IMAP_USER")
    password = os.getenv("IMAP_PASS")
    folder = os.getenv("IMAP_FOLDER", "INBOX")

    updated = 0
    try:
        with imaplib.IMAP4_SSL(host, port) as imap:
            imap.login(user, password)
            imap.select(folder)
            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                return {"ok": False, "error": "No se pudo leer el inbox de rebotes."}
            for uid in data[0].split():
                status, msg_data = imap.fetch(uid, "(RFC822)")
                if status != "OK":
                    continue
                raw = next((item[1] for item in msg_data if isinstance(item, tuple)), None)
                if not raw:
                    continue
                msg = message_from_bytes(raw)
                if not is_bounce_message(msg):
                    continue
                recipients, diagnostic = extract_bounce_data(msg)
                if not recipients:
                    continue
                matched = {email for email in recipients if email in participant_map}
                if not matched:
                    continue
                error = diagnostic or "Correo rechazado."
                for email_addr in matched:
                    participant_id = participant_map[email_addr]
                    record = EmailEnvio.query.filter_by(
                        sorteo_id=sorteo_id, participant_id=participant_id
                    ).first()
                    if record:
                        record.status = "error"
                        record.error = error
                        record.updated_at = datetime.utcnow()
                    else:
                        db.session.add(
                            EmailEnvio(
                                sorteo_id=sorteo_id,
                                participant_id=participant_id,
                                status="error",
                                error=error,
                                updated_at=datetime.utcnow(),
                            )
                        )
                    updated += 1
                imap.store(uid, "+FLAGS", "\\Seen")

        db.session.commit()
        return {"ok": True, "updated": updated}
    except Exception as exc:
        db.session.rollback()
        return {"ok": False, "error": str(exc)}


def schedule_bounce_polls(sorteo_id: int) -> None:
    if not imap_configured():
        return
    delays = [5 * 60, 2 * 60 * 60]
    for delay in delays:
        timer = threading.Timer(delay, lambda sid=sorteo_id: run_bounce_poll(sid))
        timer.daemon = True
        timer.start()


def run_bounce_poll(sorteo_id: int) -> None:
    with app.app_context():
        poll_bounces_for_draw(sorteo_id)

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


def api_key_fingerprint(api_key: str) -> str:
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def log_template_diagnostic(
    phone_number_id: str,
    api_key: str,
    base_url: str,
    role: str,
    template_name: str,
    language_code: str,
    body_order: str,
    to_contact: str,
    body_parameters: List[str],
    button_index: Optional[str] = None,
    button_value_source: Optional[str] = None,
    button_url_parameter: Optional[str] = None,
) -> None:
    diagnostic = {
        "role": role,
        "phone_number_id": phone_number_id,
        "key_fp": api_key_fingerprint(api_key),
        "base_url": base_url,
        "template": template_name,
        "lang": language_code,
        "body_order": body_order,
        "to": phone_to_wa_id(to_contact),
        "body_parameters": body_parameters,
    }
    if button_index is not None:
        diagnostic["button_index"] = button_index
    if button_value_source is not None:
        diagnostic["button_value_source"] = button_value_source
    if button_url_parameter is not None:
        diagnostic["button_url_parameter"] = button_url_parameter
    app.logger.info("KAPSO_TEMPLATE_DIAGNOSTIC %s", json.dumps(diagnostic, ensure_ascii=False))


def kapso_post_message(phone_number_id: str, api_key: str, payload: dict) -> dict:
    base_url = (os.getenv("KAPSO_BASE_URL") or "https://api.kapso.ai/meta/whatsapp/v24.0").rstrip("/")
    user_agent = (os.getenv("KAPSO_USER_AGENT") or "Sorty/1.0 (+https://sorty.com.ar)").strip()
    url = f"{base_url}/{phone_number_id}/messages"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": user_agent,
            "X-API-Key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise build_kapso_app_error(status_code=exc.code, detail=detail, payload=payload) from exc
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", "") or "").strip()
        hint = "Verifica conectividad de red y la URL base de Kapso (KAPSO_BASE_URL)."
        message = f"No se pudo conectar con Kapso. {reason}" if reason else "No se pudo conectar con Kapso."
        raise AppError(message, code="KAPSO_NETWORK", hint=hint, source="kapso") from exc


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
    sorteo_id: Optional[int] = None,
    only_emails: Optional[Set[str]] = None,
    include_admin: bool = True,
) -> Dict[str, object]:
    mode = (mode_override or os.getenv("EMAIL_MODE", "smtp")).lower()
    sender_email = os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_USER") or "sorty@example.com"
    sender_name = os.getenv("SMTP_FROM_NAME") or "Sorty"

    admin = next(p for p in participants if p["is_admin"])
    admin_contact = admin["name"]
    by_email = {p["email"]: p for p in participants}
    participant_id_by_email = {p["email"]: p.get("id") for p in participants if p.get("id")}
    logo_src, logo_inline = resolve_logo_source(prefer_inline=mode != "console")
    tracking_enabled = bool(sorteo_id) and ensure_email_tracking_table()
    only_emails_norm = {normalize_email(email) for email in only_emails} if only_emails else None

    messages: List[dict] = []

    for giver in participants:
        if only_emails_norm and normalize_email(giver["email"]) not in only_emails_norm:
            continue
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
        messages.append(
            {
                "msg": msg,
                "email": giver["email"],
                "participant_id": participant_id_by_email.get(giver["email"]),
                "kind": "participant",
            }
        )

    if include_admin:
        admin_subject, admin_html, admin_text = build_admin_email(
            assignments,
            participants,
            meta,
            sender_name,
            logo_src,
            exclusions,
            admin_link,
            code=meta.get("code"),
        )
        admin_msg = EmailMessage()
        admin_msg["Subject"] = admin_subject
        admin_msg["From"] = formataddr((sender_name, sender_email))
        admin_msg["To"] = formataddr((admin["name"], admin["email"]))
        admin_msg.set_content(admin_text)
        admin_msg.add_alternative(admin_html, subtype="html")
        attach_inline_logo(admin_msg, logo_inline)
        messages.append({"msg": admin_msg, "email": admin["email"], "participant_id": None, "kind": "admin"})

    participant_results: List[dict] = []
    participant_total = len([m for m in messages if m["kind"] == "participant"])
    participant_sent = 0
    participant_error = 0
    status_updates: List[dict] = []

    if mode == "console":
        for entry in messages:
            msg = entry["msg"]
            print("\n" + "-" * 60)
            print(f"To: {msg['To']}")
            print(f"Subject: {msg['Subject']}")
            plain = msg.get_body(preferencelist=("plain",))
            print(plain.get_content() if plain else msg.as_string())
            if entry["kind"] == "participant":
                participant_results.append(
                    {
                        "email": entry["email"],
                        "participant_id": entry["participant_id"],
                        "status": "sent",
                        "error": None,
                    }
                )
                participant_sent += 1
        return {
            "mode": mode,
            "sent": False,
            "emails": len(messages),
            "participant_total": participant_total,
            "participant_sent": participant_sent,
            "participant_error": participant_error,
            "results": participant_results,
            "errors": [],
        }

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
        for entry in messages:
            msg = entry["msg"]
            status = "sent"
            error = None
            try:
                refused = server.send_message(msg)
                if refused:
                    info = refused.get(entry["email"])
                    if info:
                        status = "error"
                        error = format_smtp_error(info[0], info[1])
                    else:
                        status = "error"
                        error = "Destinatario rechazado."
            except smtplib.SMTPRecipientsRefused as exc:
                info = exc.recipients.get(entry["email"])
                status = "error"
                if info:
                    error = format_smtp_error(info[0], info[1])
                else:
                    error = "Destinatario rechazado."
            except smtplib.SMTPException as exc:
                status = "error"
                error = str(exc)

            if entry["kind"] == "participant":
                participant_results.append(
                    {
                        "email": entry["email"],
                        "participant_id": entry["participant_id"],
                        "status": status,
                        "error": error,
                    }
                )
                if status == "sent":
                    participant_sent += 1
                else:
                    participant_error += 1
                if tracking_enabled and entry["participant_id"]:
                    status_updates.append(
                        {
                            "participant_id": entry["participant_id"],
                            "status": status,
                            "error": error,
                        }
                    )

    if tracking_enabled and status_updates:
        try:
            for update in status_updates:
                record = EmailEnvio.query.filter_by(
                    sorteo_id=sorteo_id, participant_id=update["participant_id"]
                ).first()
                if record:
                    record.status = update["status"]
                    record.error = update["error"]
                    record.updated_at = datetime.utcnow()
                else:
                    db.session.add(
                        EmailEnvio(
                            sorteo_id=sorteo_id,
                            participant_id=update["participant_id"],
                            status=update["status"],
                            error=update["error"],
                            updated_at=datetime.utcnow(),
                        )
                    )
            db.session.commit()
        except Exception:
            db.session.rollback()

    error_list = [r for r in participant_results if r["status"] == "error"]
    return {
        "mode": mode,
        "sent": True,
        "emails": len(messages),
        "participant_total": participant_total,
        "participant_sent": participant_sent,
        "participant_error": participant_error,
        "results": participant_results,
        "errors": error_list,
    }


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
    base_url = (os.getenv("KAPSO_BASE_URL") or "https://api.kapso.ai/meta/whatsapp/v24.0").rstrip("/")

    if whatsapp_templates_enabled():
        default_language = (os.getenv("KAPSO_TEMPLATE_LANGUAGE") or "es_AR").strip()
        participant_template_name = (os.getenv("KAPSO_TEMPLATE_PARTICIPANT_NAME") or "amigo_invisible_confirmacion").strip()
        admin_template_name = (os.getenv("KAPSO_TEMPLATE_ADMIN_NAME") or "amigo_invisible_resultados").strip()
        participant_language = (os.getenv("KAPSO_TEMPLATE_PARTICIPANT_LANGUAGE") or default_language).strip()
        admin_language = (os.getenv("KAPSO_TEMPLATE_ADMIN_LANGUAGE") or default_language).strip()
        participant_body_order = (
            os.getenv("KAPSO_TEMPLATE_PARTICIPANT_BODY_ORDER")
            or "giver_name,receiver_name,budget,deadline,note,admin_name"
        ).strip()
        admin_body_order = (os.getenv("KAPSO_TEMPLATE_ADMIN_BODY_ORDER") or "code").strip()
        admin_button_index = (os.getenv("KAPSO_TEMPLATE_ADMIN_BUTTON_INDEX") or "").strip()
        admin_button_value_source = (os.getenv("KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE_SOURCE") or "draw_link").strip().lower()

        if not participant_template_name:
            raise AppError("Falta KAPSO_TEMPLATE_PARTICIPANT_NAME.")
        if not admin_template_name:
            raise AppError("Falta KAPSO_TEMPLATE_ADMIN_NAME.")
        if not participant_language or not admin_language:
            raise AppError("Falta idioma de template. Define KAPSO_TEMPLATE_LANGUAGE (ej. es_AR).")
        if admin_button_index and not admin_button_index.isdigit():
            raise AppError("KAPSO_TEMPLATE_ADMIN_BUTTON_INDEX debe ser numerico (ej. 0).")
        if admin_button_value_source not in {"draw_link", "code", "env"}:
            raise AppError("KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE_SOURCE invalido. Usa draw_link, code o env.")

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
            log_template_diagnostic(
                phone_number_id=phone_number_id,
                api_key=api_key,
                base_url=base_url,
                role="participant",
                template_name=participant_template_name,
                language_code=participant_language,
                body_order=participant_body_order,
                to_contact=giver["email"],
                body_parameters=body_parameters,
            )
            try:
                kapso_send_template(
                    phone_number_id=phone_number_id,
                    api_key=api_key,
                    to_contact=giver["email"],
                    template_name=participant_template_name,
                    language_code=participant_language,
                    body_parameters=body_parameters,
                )
            except AppError as exc:
                raise AppError(
                    f"Fallo envio WhatsApp a participante '{giver['name']}' ({giver['email']}). {exc.message}",
                    code=exc.code,
                    hint=exc.hint,
                    source=exc.source,
                ) from exc
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
            if admin_button_value_source == "env" and not (os.getenv("KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE") or "").strip():
                raise AppError("Falta KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE para boton dinamico de admin.")

        log_template_diagnostic(
            phone_number_id=phone_number_id,
            api_key=api_key,
            base_url=base_url,
            role="admin",
            template_name=admin_template_name,
            language_code=admin_language,
            body_order=admin_body_order,
            to_contact=admin["email"],
            body_parameters=admin_body_parameters,
            button_index=admin_button_index,
            button_value_source=admin_button_value_source,
            button_url_parameter=admin_button_parameter,
        )
        try:
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
        except AppError as exc:
            raise AppError(
                f"Fallo envio WhatsApp al Administrador '{admin['name']}' ({admin['email']}). {exc.message}",
                code=exc.code,
                hint=exc.hint,
                source=exc.source,
            ) from exc
        sent_count += 1
        return {"mode": mode, "sent": True, "emails": sent_count, "transport": "kapso-template"}

    sent_count = 0
    for to_contact, to_name, text in outbound:
        try:
            kapso_send_text(phone_number_id=phone_number_id, api_key=api_key, to_contact=to_contact, body=text)
        except AppError as exc:
            raise AppError(
                f"Fallo envio WhatsApp a '{to_name}' ({to_contact}). {exc.message}",
                code=exc.code,
                hint=exc.hint,
                source=exc.source,
            ) from exc
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
    sorteo_id: Optional[int] = None,
    only_emails: Optional[Set[str]] = None,
    include_admin: bool = True,
) -> Dict[str, object]:
    if channel == "whatsapp":
        if only_emails or not include_admin:
            raise AppError("El reenvio individual solo aplica al canal email.")
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
        sorteo_id=sorteo_id,
        only_emails=only_emails,
        include_admin=include_admin,
    )


def build_sorteo_link(sorteo: Sorteo) -> str:
    """Return an absolute link to the stored draw (sorteo)."""
    base = resolve_public_base_url()
    return f"{base}/sorteo/{sorteo.code}"


def load_draw_data(code: str) -> Tuple[Sorteo, dict]:
    """Fetch draw data (participants, exclusions, assignments) by code or UUID."""
    ensure_database_schema()
    raw_code = (code or "").strip()
    if "{{" in raw_code or "}}" in raw_code:
        raise AppError(
            "El link recibido contiene un placeholder sin reemplazar ({{1}}). "
            "Actualiza el template de WhatsApp para usar boton URL dinamico."
        )

    normalized_code = re.sub(r"[^A-Za-z0-9]", "", raw_code).upper()
    query = Sorteo.query.options(
        joinedload(Sorteo.participantes).joinedload(Participante.exclusiones),
        joinedload(Sorteo.asignaciones).joinedload(Asignacion.giver),
        joinedload(Sorteo.asignaciones).joinedload(Asignacion.receiver),
    )

    sorteo = query.filter(Sorteo.code == raw_code).first()
    if not sorteo and normalized_code and normalized_code != raw_code:
        sorteo = query.filter(Sorteo.code == normalized_code).first()
    if not sorteo:
        try:
            sorteo = query.filter(Sorteo.public_id == uuid.UUID(raw_code)).first()
        except ValueError:
            sorteo = None
    if not sorteo:
        raise AppError("Sorteo no encontrado.")

    channel = infer_channel_from_contact(sorteo.admin_contact)
    participants = []
    for p in sorteo.participantes:
        participants.append(
            {
                "id": p.id,
                "name": p.nombre,
                "email": p.email,
                "contact": p.email,
                "is_admin": p.email.lower() == sorteo.admin_contact.lower(),
            }
        )
    participant_by_email = {p["email"]: p for p in participants}

    exclusions: List[Tuple[str, str]] = []
    for p in sorteo.participantes:
        for excl in p.exclusiones:
            exclusions.append((p.email, excl.email))

    assignments: Dict[str, str] = {}
    for a in sorteo.asignaciones:
        assignments[a.giver.email] = a.receiver.email

    status_map: Dict[int, dict] = {}
    try:
        inspector = inspect(db.engine)
        if "email_envio" in inspector.get_table_names():
            for record in EmailEnvio.query.filter_by(sorteo_id=sorteo.id).all():
                status_map[record.participant_id] = {"status": record.status, "error": record.error}
    except Exception:
        status_map = {}

    payload = {
        "id": str(sorteo.public_id),
        "code": sorteo.code,
        "name": sorteo.nombre,
        "channel": channel,
        "email_admin": sorteo.admin_contact,
        "admin_contact": sorteo.admin_contact,
        "participants": participants,
        "exclusions": exclusions,
        "assignments": [
            {
                "giver_email": giver,
                "receiver_email": receiver,
                "giver_contact": giver,
                "receiver_contact": receiver,
                "giver_name": participant_by_email.get(giver, {}).get("name", giver),
                "receiver_name": participant_by_email.get(receiver, {}).get("name", receiver),
                "giver_id": participant_by_email.get(giver, {}).get("id"),
                "receiver_id": participant_by_email.get(receiver, {}).get("id"),
                "email_status": status_map.get(participant_by_email.get(giver, {}).get("id", -1), {}).get("status"),
                "email_error": status_map.get(participant_by_email.get(giver, {}).get("id", -1), {}).get("error"),
            }
            for giver, receiver in assignments.items()
        ],
    }
    return sorteo, payload


def update_participant_contact(sorteo: Sorteo, participant_id: int, new_contact: str, notify_previous: bool) -> dict:
    draw_channel = infer_channel_from_contact(sorteo.admin_contact)
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
    if sorteo.admin_contact.lower() == old_contact.lower():
        sorteo.admin_contact = new_contact_norm
        admin_contact_updated = True

    db.session.commit()

    try:
        inspector = inspect(db.engine)
        if "email_envio" in inspector.get_table_names():
            EmailEnvio.query.filter_by(sorteo_id=sorteo.id, participant_id=participant.id).delete()
            db.session.commit()
    except Exception:
        db.session.rollback()

    notified = False
    if notify_previous and old_contact.lower() != new_contact_norm.lower() and draw_channel == "email":
        admin_contact = f"{sorteo.nombre} ({sorteo.admin_contact})"
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
        "is_admin": participant.email.lower() == sorteo.admin_contact.lower(),
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

    sorteo = Sorteo(nombre=name, admin_contact=admin["email"], estado="finalizado")
    db.session.add(sorteo)

    by_email: Dict[str, Participante] = {}
    for p in participants:
        record = Participante(sorteo=sorteo, nombre=p["name"], email=p["email"])
        db.session.add(record)
        by_email[p["email"]] = record

    db.session.flush()
    for p in participants:
        record = by_email.get(p["email"])
        if record:
            p["id"] = record.id

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
    base = resolve_public_base_url()
    return render_template(
        "landing.html",
        canonical_url=f"{base}/",
        app_url=f"{base}/app",
        og_image_url=f"{base}/static/sorty_logo.png",
    )


@app.route("/app", methods=["GET"])
def index():
    base = resolve_public_base_url()
    email_mode = os.getenv("EMAIL_MODE", "smtp").lower()
    whatsapp_mode = os.getenv("WHATSAPP_MODE", "kapso").lower()
    return render_template(
        "index.html",
        email_mode=email_mode,
        whatsapp_mode=whatsapp_mode,
        canonical_url=f"{base}/app",
        og_image_url=f"{base}/static/sorty_logo.png",
    )


@app.route("/robots.txt", methods=["GET"])
def robots_txt():
    base = resolve_public_base_url()
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /preview/",
        "Disallow: /sorteo/",
        "Disallow: /draw/",
        f"Sitemap: {base}/sitemap.xml",
    ]
    return Response("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")


@app.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    base = resolve_public_base_url()
    today = datetime.utcnow().date().isoformat()
    entries = [
        {"loc": f"{base}/", "changefreq": "weekly", "priority": "1.0"},
        {"loc": f"{base}/app", "changefreq": "weekly", "priority": "0.9"},
    ]
    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in entries:
        xml_lines.extend(
            [
                "  <url>",
                f"    <loc>{xml_escape(entry['loc'])}</loc>",
                f"    <lastmod>{today}</lastmod>",
                f"    <changefreq>{entry['changefreq']}</changefreq>",
                f"    <priority>{entry['priority']}</priority>",
                "  </url>",
            ]
        )
    xml_lines.append("</urlset>")
    return Response("\n".join(xml_lines), content_type="application/xml; charset=utf-8")


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
                sorteo_id=sorteo_record.id if sorteo_record else None,
            )
            if (
                channel == "email"
                and delivery_status
                and delivery_status.get("mode") == "smtp"
                and sorteo_record
            ):
                schedule_bounce_polls(sorteo_record.id)

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
        log_api_app_error(endpoint="/api/sorteo", payload=payload, exc=exc)
        return jsonify(error_payload_from_exception(exc)), 400
    except Exception as exc:  # pragma: no cover - safety net
        app.logger.exception("API_UNHANDLED_ERROR endpoint=/api/sorteo")
        return jsonify(error_payload_from_exception(exc)), 500


@app.route("/api/sorteo/<code>", methods=["GET"])
@app.route("/api/draw/<code>", methods=["GET"])  # alias antiguo
def api_draw_get(code: str):
    try:
        _, data = load_draw_data(code)
        return jsonify({"ok": True, "draw": data})
    except AppError as exc:
        return jsonify(error_payload_from_exception(exc)), 404
    except Exception as exc:
        return jsonify(error_payload_from_exception(exc)), 500


@app.route("/api/sorteo/<code>/resend", methods=["POST"])
@app.route("/api/draw/<code>/resend", methods=["POST"])  # alias antiguo
def api_draw_resend(code: str):
    payload = request.get_json(silent=True) or {}
    mode_override = (payload.get("mode") or "").strip().lower() or None
    try:
        sorteo, data = load_draw_data(code)
        admin_contact = data.get("admin_contact") or data.get("email_admin") or ""
        channel = normalize_channel(data.get("channel") or infer_channel_from_contact(admin_contact))
        if mode_override:
            allowed_modes = {"console", "smtp"} if channel == "email" else {"console", "kapso"}
            if mode_override not in allowed_modes:
                if channel == "email":
                    raise AppError("Modo de email invalido. Usa console o smtp.")
                raise AppError("Modo de WhatsApp invalido. Usa console o kapso.")
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
            sorteo_id=sorteo.id,
        )
        message = "Correos reenviados." if channel == "email" else "Mensajes de WhatsApp reenviados."
        if channel == "email" and delivery_status and delivery_status.get("mode") == "smtp":
            schedule_bounce_polls(sorteo.id)
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
        log_api_app_error(endpoint="/api/sorteo/<code>/resend", payload=payload, exc=exc)
        return jsonify(error_payload_from_exception(exc)), 400
    except Exception as exc:
        app.logger.exception("API_UNHANDLED_ERROR endpoint=/api/sorteo/<code>/resend")
        return jsonify(error_payload_from_exception(exc)), 500


@app.route("/api/sorteo/<code>/participant/<int:participant_id>/resend", methods=["POST"])
@app.route("/api/draw/<code>/participant/<int:participant_id>/resend", methods=["POST"])  # alias antiguo
def api_draw_resend_participant(code: str, participant_id: int):
    payload = request.get_json(silent=True) or {}
    mode_override = (payload.get("mode") or "").strip().lower() or None
    try:
        sorteo, data = load_draw_data(code)
        admin_contact = data.get("admin_contact") or data.get("email_admin") or ""
        channel = normalize_channel(data.get("channel") or infer_channel_from_contact(admin_contact))
        if channel != "email":
            raise AppError("El reenvio individual solo esta disponible para sorteos por email.")
        if mode_override and mode_override not in {"console", "smtp"}:
            raise AppError("Modo de email invalido. Usa console o smtp.")
        participants = data["participants"]
        exclusions = data["exclusions"]
        assignments = {item["giver_email"]: item["receiver_email"] for item in data["assignments"]}

        participant = next((p for p in participants if p["id"] == participant_id), None)
        if not participant:
            raise AppError("Participante no encontrado en este sorteo.")

        admin = next((p for p in participants if p["is_admin"]), None)
        if not admin:
            raise AppError("No se encontro Administrador para este sorteo.")

        meta_clean = {
            "budget": "",
            "deadline": "",
            "note": "",
            "admin": admin,
            "code": sorteo.code,
        }
        email_status = dispatch_emails(
            participants,
            assignments,
            meta_clean,
            exclusions,
            mode_override,
            admin_link=build_sorteo_link(sorteo),
            sorteo_id=sorteo.id,
            only_emails={participant["email"]},
            include_admin=False,
        )
        if email_status and email_status.get("mode") == "smtp":
            schedule_bounce_polls(sorteo.id)
        return jsonify({"ok": True, "message": "Correo reenviado.", "email_status": email_status})
    except AppError as exc:
        return jsonify(error_payload_from_exception(exc)), 400
    except Exception as exc:
        return jsonify(error_payload_from_exception(exc)), 500


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
        return jsonify(error_payload_from_exception(exc)), 400
    except Exception as exc:
        return jsonify(error_payload_from_exception(exc)), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    url = f"http://localhost:{port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port, debug=True)
