import os
import random
import smtplib
import ssl
import threading
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


class AppError(Exception):
    """Errors that should be surfaced to the client with a friendly message."""


def normalize_email(value: str) -> str:
    return value.strip().lower()


def validate_participants(raw: List[dict]) -> List[dict]:
    participants: List[dict] = []
    seen_emails: Set[str] = set()
    admin_count = 0

    for item in raw:
        name = (item.get("name") or "").strip()
        email = normalize_email(item.get("email") or "")
        is_admin = bool(item.get("is_admin"))

        if not name:
            raise AppError("Cada participante necesita un nombre.")
        if "@" not in email or "." not in email:
            raise AppError(f"Email invalido para {name}.")
        if email in seen_emails:
            raise AppError(f"Email duplicado: {email}")

        seen_emails.add(email)
        admin_count += 1 if is_admin else 0
        participants.append({"name": name, "email": email, "is_admin": is_admin})

    if len(participants) < 3:
        raise AppError("Carga al menos tres participantes.")
    if admin_count != 1:
        raise AppError("Selecciona exactamente un Administrador.")

    return participants


def validate_exclusions(
    raw: List[dict], allowed_emails: Set[str]
) -> List[Tuple[str, str]]:
    exclusions: List[Tuple[str, str]] = []
    for item in raw:
        giver = normalize_email(item.get("from") or "")
        receiver = normalize_email(item.get("to") or "")
        if not giver or not receiver:
            continue
        if giver not in allowed_emails or receiver not in allowed_emails:
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


def build_participant_email(
    giver: dict, receiver: dict, meta: dict, admin_contact: str, sender_name: str
) -> Tuple[str, str, str]:
    budget = meta.get("budget")
    deadline = meta.get("deadline")
    note = meta.get("note")

    base = (os.getenv("PUBLIC_APP_URL") or "").strip().rstrip("/")
    if not base:
        try:
            base = (request.url_root or "").rstrip("/")
        except RuntimeError:
            base = "http://localhost:5000"
    if base.startswith("http://"):
        base = "https://" + base[len("http://") :]
    logo_url = f"{base}/static/sorty_logo.png"

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
        logo_url=logo_url,
        max_width=520,
    )
    return subject_line, html_body, text_body


def build_admin_email(
    assignments: Dict[str, str],
    participants: List[dict],
    meta: dict,
    sender_name: str,
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

    messages: List[EmailMessage] = []

    for giver in participants:
        receiver = by_email[assignments[giver["email"]]]
        subject, html_body, text_body = build_participant_email(
            giver, receiver, meta, admin_contact, sender_name
        )
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((sender_name, sender_email))
        msg["To"] = formataddr((giver["name"], giver["email"]))
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        messages.append(msg)

    admin_subject, admin_html, admin_text = build_admin_email(
        assignments, participants, meta, sender_name, exclusions, admin_link, code=meta.get("code")
    )
    admin_msg = EmailMessage()
    admin_msg["Subject"] = admin_subject
    admin_msg["From"] = formataddr((sender_name, sender_email))
    admin_msg["To"] = formataddr((admin["name"], admin["email"]))
    admin_msg.set_content(admin_text)
    admin_msg.add_alternative(admin_html, subtype="html")
    messages.append(admin_msg)

    if mode == "console":
        for msg in messages:
            print("\n" + "-" * 60)
            print(f"To: {msg['To']}")
            print(f"Subject: {msg['Subject']}")
            print(msg.get_content())
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
        print(msg.get_content())
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

    participants = []
    for p in sorteo.participantes:
        participants.append(
            {"id": p.id, "name": p.nombre, "email": p.email, "is_admin": p.email.lower() == sorteo.email_admin.lower()}
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
        "email_admin": sorteo.email_admin,
        "participants": participants,
        "exclusions": exclusions,
        "assignments": [
            {
                "giver_email": giver,
                "receiver_email": receiver,
                "giver_name": next((p["name"] for p in participants if p["email"] == giver), giver),
                "receiver_name": next((p["name"] for p in participants if p["email"] == receiver), receiver),
            }
            for giver, receiver in assignments.items()
        ],
    }
    return sorteo, payload


def update_participant_email(sorteo: Sorteo, participant_id: int, new_email: str, notify_previous: bool) -> dict:
    new_email_norm = normalize_email(new_email)
    if "@" not in new_email_norm or "." not in new_email_norm:
        raise AppError("Email invalido.")

    participant = next((p for p in sorteo.participantes if p.id == participant_id), None)
    if not participant:
        raise AppError("Participante no encontrado en este sorteo.")

    if any(p.email.lower() == new_email_norm and p.id != participant.id for p in sorteo.participantes):
        raise AppError("Ya existe un participante con ese email.")

    old_email = participant.email
    participant.email = new_email_norm

    admin_email_updated = False
    if sorteo.email_admin.lower() == old_email.lower():
        sorteo.email_admin = new_email_norm
        admin_email_updated = True

    db.session.commit()

    notified = False
    if notify_previous and old_email.lower() != new_email_norm.lower():
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
        send_simple_email(old_email, participant.nombre, subject, text_body, html_body)
        notified = True

    return {
        "id": participant.id,
        "name": participant.nombre,
        "email": participant.email,
        "is_admin": participant.email.lower() == sorteo.email_admin.lower(),
        "notified_previous": notified,
        "admin_email_updated": admin_email_updated,
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
                "receiver_name": receiver["name"],
                "receiver_email": receiver["email"],
            }
        )
    return rendered


@app.route("/", methods=["GET"])
def landing():
    base = (os.getenv("PUBLIC_APP_URL") or request.url_root or "").strip().rstrip("/")
    if not base:
        base = "https://sorty-neon.vercel.app"
    if base.startswith("http://"):
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
    return render_template("index.html", email_mode=email_mode)


@app.route("/preview/email/participant", methods=["GET"])
def preview_participant_email():
    giver = {"name": "Carla", "email": "carla@example.com"}
    receiver = {"name": "Pablo", "email": "pablo@example.com"}
    meta = {
        "budget": "20 USD",
        "deadline": "20/12",
        "note": "Traer regalo envuelto.",
    }
    _, html_body, _ = build_participant_email(
        giver,
        receiver,
        meta,
        admin_contact="Nora (admin)",
        sender_name="Sorty",
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
    _, html_body, _ = build_admin_email(
        assignments,
        participants,
        meta,
        sender_name="Sorty",
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
    send_emails = bool(payload.get("send"))
    meta = payload.get("meta") or {}
    mode_override = (payload.get("mode") or "").strip().lower() or None

    try:
        participants = validate_participants(payload.get("participants") or [])
        exclusions = validate_exclusions(payload.get("exclusions") or [], {p["email"] for p in participants})
        assignments, err = find_assignments(participants, exclusions)
        if err or not assignments:
            raise AppError(err or "No se pudo generar un sorteo valido.")

        if mode_override and mode_override not in {"console", "smtp"}:
            raise AppError("Modo de email invalido. Usa console o smtp.")

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

        email_status = None
        if send_emails:
            email_status = dispatch_emails(
                participants, assignments, meta_clean, exclusions, mode_override, admin_link=draw_link
            )

        response = {
            "ok": True,
            "message": "Correos enviados." if send_emails else "Simulacion lista. No se enviaron correos.",
            "assignment": assignment_for_client(assignments, participants),
            "email_status": email_status,
            "mode": mode_override or os.getenv("EMAIL_MODE", "smtp"),
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
        }
        email_status = dispatch_emails(
            participants,
            assignments,
            meta_clean,
            exclusions,
            mode_override,
            admin_link=build_sorteo_link(sorteo),
        )
        return jsonify({"ok": True, "message": "Correos reenviados.", "email_status": email_status})
    except AppError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Error inesperado en el servidor."}), 500


@app.route("/api/sorteo/<code>/participant/<int:participant_id>/email", methods=["PATCH"])
@app.route("/api/draw/<code>/participant/<int:participant_id>/email", methods=["PATCH"])  # alias antiguo
def api_draw_update_email(code: str, participant_id: int):
    payload = request.get_json(force=True) or {}
    new_email = (payload.get("email") or "").strip()
    notify_previous = bool(payload.get("notify_previous"))
    try:
        sorteo, _ = load_draw_data(code)
        updated = update_participant_email(sorteo, participant_id, new_email, notify_previous)
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
