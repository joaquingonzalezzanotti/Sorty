# Sorty

Sorty es una app web para organizar sorteos de amigo invisible con un flujo simple: cargas participantes, defines exclusiones, generas asignaciones validas y envias notificaciones por email o WhatsApp.

## Estado actual

- `https://sorty-neon.vercel.app/` muestra la landing (marketing + SEO).
- `https://sorty-neon.vercel.app/app` muestra la app operativa para crear el sorteo.
- Cada sorteo guardado tiene una vista de administracion en `/sorteo/<code>` (alias legado: `/draw/<code>`).

## Funcionalidades principales

- Landing en `/` con metadata SEO (title, description, Open Graph, Twitter y JSON-LD).
- Formulario en `/app` con:
  - selector de canal (`email` o `whatsapp`),
  - participantes (nombre + email o telefono en formato E.164),
  - administrador unico,
  - exclusiones personalizadas,
  - presupuesto, fecha limite y nota grupal.
- Validaciones de negocio:
  - minimo 3 participantes,
  - contactos validos y sin duplicados (email o WhatsApp),
  - 1 solo administrador,
  - sin autoasignacion,
  - control de restricciones imposibles.
- Generacion de asignaciones con backtracking.
- Persistencia de sorteos en base de datos (codigo publico y UUID).
- Envio individual a cada participante por el canal elegido.
- En WhatsApp: el admin recibe el link de gestion del sorteo (sin detalle de asignaciones en el mensaje).
- Vista admin de sorteo con:
  - copiar link,
  - reenviar mensajes,
  - corregir contacto de participante,
  - opcion de notificar al correo anterior.

## Stack

- Python + Flask
- Flask-SQLAlchemy
- PostgreSQL (Neon/Vercel) o SQLite local como fallback
- Frontend server-rendered (Jinja + CSS + JS vanilla)
- Deploy en Vercel (`api/index.py` expone la app WSGI)


## API principal

- `POST /api/sorteo` (alias: `POST /api/draw`): crea sorteo, guarda datos y opcionalmente envia mensajes.
- `GET /api/sorteo/<code>` (alias: `GET /api/draw/<code>`): obtiene datos del sorteo.
- `POST /api/sorteo/<code>/resend` (alias: `POST /api/draw/<code>/resend`): reenvia mensajes por el canal del sorteo.
- `PATCH /api/sorteo/<code>/participant/<id>/contact` (alias legado `.../email`): corrige contacto de participante.

## Variables de entorno

- `EMAIL_MODE=smtp|console`
- `SMTP_USER`, `SMTP_PASS`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`
- `WHATSAPP_MODE=kapso|console`
- `KAPSO_API_KEY` (obligatoria para modo `kapso`)
- `KAPSO_PHONE_NUMBER_ID` (opcional, por defecto `1116659434858231`)
- `KAPSO_BASE_URL` (opcional, por defecto `https://api.kapso.ai/meta/whatsapp/v24.0`)
- `WHATSAPP_USE_TEMPLATES=1|0` (por defecto `1`; en `kapso` envia `type: template`)
- `KAPSO_TEMPLATE_LANGUAGE` (por defecto `es_MX`)
- `KAPSO_TEMPLATE_PARTICIPANT_NAME` (por defecto `amigo_invisible_confirmacion`)
- `KAPSO_TEMPLATE_ADMIN_NAME` (por defecto `amigo_invisible_results`)
- `KAPSO_TEMPLATE_PARTICIPANT_LANGUAGE` (opcional; pisa idioma general para participante)
- `KAPSO_TEMPLATE_ADMIN_LANGUAGE` (opcional; pisa idioma general para admin)
- `KAPSO_TEMPLATE_PARTICIPANT_BODY_ORDER` (opcional; por defecto `receiver_name,budget,deadline,note,admin_name,code`)
- `KAPSO_TEMPLATE_ADMIN_BODY_ORDER` (opcional; por defecto `code,budget,deadline,note`)
- `KAPSO_TEMPLATE_ADMIN_BUTTON_INDEX` (opcional; si tu template de admin tiene boton URL dinamico, ej `0`)
- `KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE_SOURCE=draw_link|code|env` (opcional; por defecto `draw_link`)
- `KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE` (opcional; usado cuando `...VALUE_SOURCE=env`)

