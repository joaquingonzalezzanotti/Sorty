import os
import random
import smtplib
import ssl
import threading
import webbrowser
from email.message import EmailMessage
from email.utils import formataddr
from typing import Dict, List, Optional, Set, Tuple

from flask import Flask, jsonify, render_template, request

from models import Asignacion, Participante, Sorteo, db


def resolve_database_uri() -> str:
    """Pick the first available connection string, supporting Vercel + Neon defaults."""
    candidates = [
        os.getenv("DATABASE_URL"),
        os.getenv("POSTGRES_PRISMA_URL"),  # Vercel + Neon (pooled)
        os.getenv("POSTGRES_URL"),  # Vercel + Neon (pooled)
        os.getenv("POSTGRES_URL_NON_POOLING"),
    ]
    for uri in candidates:
        if uri:
            return uri.replace("postgres://", "postgresql://", 1)
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

    subject_line = "Tu sorteo (Sorty)" + (f" - Entrega antes de {deadline}" if deadline else "")

    text_lines = [
        f"Hola {giver['name']},",
        "",
        f"Te toco regalar a: {receiver['name']} ({receiver['email']}).",
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

    # Inline styles para compatibilidad en emails.
    html_parts = [
        "<!doctype html>",
        "<html>",
        "<body style=\"font-family:'Segoe UI', Arial, sans-serif; background:#f6f8fb; padding:24px; color:#1d2433;\">",
        "<div style=\"max-width:520px; margin:0 auto; background:#ffffff; border-radius:14px; padding:24px; box-shadow:0 12px 40px rgba(0,0,0,0.08);\">",
        f"<div style=\"font-size:14px; letter-spacing:0.4px; text-transform:uppercase; color:#5f6b7a;\">Sorty</div>",
        f"<h2 style=\"margin:12px 0 10px; font-size:24px; color:#111827;\">Hola {giver['name']},</h2>",
        f"<p style=\"font-size:16px; line-height:1.6; margin:12px 0;\">Te toco regalar a <strong>{receiver['name']}</strong> (<a style=\"color:#0e7490; text-decoration:none;\" href=\"mailto:{receiver['email']}\">{receiver['email']}</a>).</p>",
    ]

    if budget:
        html_parts.append(f"<p style=\"margin:8px 0; font-size:15px;\">Presupuesto sugerido: <strong>{budget}</strong></p>")
    if deadline:
        html_parts.append(f"<p style=\"margin:8px 0; font-size:15px;\">Fecha limite: <strong>{deadline}</strong></p>")
    if note:
        html_parts.append(
            f"<div style=\"margin:12px 0; padding:12px 14px; border-left:4px solid #0ea5e9; background:#f0f9ff; border-radius:8px; font-size:15px; line-height:1.5;\">"
            f"<div style=\"font-weight:600; color:#0f172a;\">Mensaje del grupo:</div>"
            f"<div style=\"margin-top:6px; color:#0f172a;\">{note}</div>"
            f"</div>"
        )

    html_parts.append(
        f"<p style=\"margin:14px 0 8px; font-size:15px;\">Si necesitas algo, contacta a <strong>{admin_contact}</strong>.</p>"
    )
    html_parts.append(
        "<div style=\"margin-top:18px; padding:12px 14px; background:linear-gradient(135deg,#0ea5e9,#06b6d4); color:white; border-radius:10px; font-size:15px;\">"
        "Disfruta la sorpresa!"
        "</div>"
    )
    html_parts.append(
        f"<p style=\"margin-top:14px; font-size:13px; color:#6b7280;\">Enviado por {sender_name} via Sorty.</p>"
    )
    html_parts.append("</div></body></html>")

    html_body = "".join(html_parts)
    return subject_line, html_body, text_body


def build_admin_email(
    assignments: Dict[str, str],
    participants: List[dict],
    meta: dict,
    sender_name: str,
    exclusions: List[Tuple[str, str]],
) -> Tuple[str, str, str]:
    budget = meta.get("budget")
    deadline = meta.get("deadline")
    note = meta.get("note")

    by_email = {p["email"]: p for p in participants}
    rows_html = []
    rows_text = []
    for giver_email, receiver_email in assignments.items():
        giver = by_email[giver_email]
        receiver = by_email[receiver_email]
        rows_html.append(
            f"<tr><td style='padding:8px 10px; border-bottom:1px solid #e5e7eb;'>{giver['name']}</td>"
            f"<td style='padding:8px 10px; border-bottom:1px solid #e5e7eb; color:#0ea5e9;'>{receiver['name']}</td></tr>"
        )
        rows_text.append(f"{giver['name']} -> {receiver['name']} ({receiver['email']})")

    subject = "Resultados del Sorteo - Administrador"
    html_parts = [
        "<!doctype html><html><body style=\"font-family:'Segoe UI', Arial, sans-serif; background:#f6f8fb; padding:24px; color:#1d2433;\">",
        "<div style=\"max-width:560px; margin:0 auto; background:#ffffff; border-radius:14px; padding:24px; box-shadow:0 12px 40px rgba(0,0,0,0.08);\">",
        "<div style=\"font-size:14px; letter-spacing:0.4px; text-transform:uppercase; color:#5f6b7a;\">Administrador</div>",
        "<h2 style=\"margin:12px 0 10px; font-size:22px; color:#111827;\">Asignaciones Completas</h2>",
        "<p style='font-size:15px;'>Este correo se envía porque usted es el Administrador del sorteo y contiene todos los resultados. Si desea ver únicamente a quién le ha tocado, por favor revise el otro correo.</p>",
        "<table style='width:100%; border-collapse:collapse; margin-top:10px; font-size:15px;'>",
        "<thead><tr><th style='text-align:left; padding:8px 10px; color:#6b7280;'>Entrega</th><th style='text-align:left; padding:8px 10px; color:#6b7280;'>Para</th></tr></thead>",
        "<tbody>",
        "".join(rows_html),
        "</tbody></table>",
    ]

    if exclusions:
        html_parts.append(
            "<div style='margin-top:16px; padding:12px 14px; background:#0f172a; border:1px solid #1f2937; border-radius:10px;'>"
            "<div style='font-weight:600; color:#e5e7eb; margin-bottom:6px;'>Exclusiones cargadas</div>"
        )
        for giver_email, receiver_email in exclusions:
            giver = by_email.get(giver_email)
            receiver = by_email.get(receiver_email)
            if giver and receiver:
                html_parts.append(
                    f"<div style='color:#cbd5e1; font-size:14px; margin:4px 0;'>{giver['name']} no regala a {receiver['name']}</div>"
                )
        html_parts.append("</div>")

    if budget or deadline or note:
        html_parts.append("<div style='margin-top:16px; padding:12px 14px; background:#f9fafb; border-radius:10px; border:1px solid #e5e7eb;'>")
        if budget:
            html_parts.append(f"<div style='margin:4px 0;'><strong>Presupuesto:</strong> {budget}</div>")
        if deadline:
            html_parts.append(f"<div style='margin:4px 0;'><strong>Fecha limite:</strong> {deadline}</div>")
        if note:
            html_parts.append(f"<div style='margin:4px 0;'><strong>Mensaje:</strong> {note}</div>")
        html_parts.append("</div>")

    html_parts.append(
        f"<p style='margin-top:16px; font-size:13px; color:#6b7280;'>Enviado por {sender_name} via Sorty.</p>"
    )
    html_parts.append("</div></body></html>")

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

    html_body = "".join(html_parts)
    text_body = "\n".join(text_lines)
    return subject, html_body, text_body


def dispatch_emails(
    participants: List[dict],
    assignments: Dict[str, str],
    meta: dict,
    exclusions: List[Tuple[str, str]],
    mode_override: Optional[str] = None,
) -> Dict[str, object]:
    mode = (mode_override or os.getenv("EMAIL_MODE", "smtp")).lower()
    sender_email = os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_USER") or "sorty@example.com"
    sender_name = os.getenv("SMTP_FROM_NAME") or "Sorty"

    admin = next(p for p in participants if p["is_admin"])
    admin_contact = f"{admin['name']} ({admin['email']})"
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
        assignments, participants, meta, sender_name, exclusions
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
def index():
    email_mode = os.getenv("EMAIL_MODE", "smtp").lower()
    return render_template("index.html", email_mode=email_mode)


@app.route("/api/draw", methods=["POST"])
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

        email_status = None
        if send_emails:
            email_status = dispatch_emails(participants, assignments, meta_clean, exclusions, mode_override)

        response = {
            "ok": True,
            "message": "Correos enviados." if send_emails else "Simulacion lista. No se enviaron correos.",
            "assignment": assignment_for_client(assignments, participants),
            "email_status": email_status,
            "mode": mode_override or os.getenv("EMAIL_MODE", "smtp"),
        }
        return jsonify(response)

    except AppError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - safety net
        return jsonify({"ok": False, "error": "Error inesperado en el servidor."}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    url = f"http://localhost:{port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port, debug=True)
