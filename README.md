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
- PostgreSQL (Neon/Vercel) en production; SQLite solo para desarrollo local
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
- `KAPSO_USER_AGENT` (opcional, por defecto `Sorty/1.0 (+https://sorty.com.ar)`)
- `WHATSAPP_USE_TEMPLATES=1|0` (por defecto `1`; en `kapso` envia `type: template`)
- `KAPSO_TEMPLATE_LANGUAGE` (por defecto `es_AR`)
- `KAPSO_TEMPLATE_PARTICIPANT_NAME` (por defecto `amigo_invisible_confirmacion`)
- `KAPSO_TEMPLATE_ADMIN_NAME` (por defecto `amigo_invisible_results`)
- `KAPSO_TEMPLATE_PARTICIPANT_LANGUAGE` (opcional; pisa idioma general para participante)
- `KAPSO_TEMPLATE_ADMIN_LANGUAGE` (opcional; pisa idioma general para admin)
- `KAPSO_TEMPLATE_PARTICIPANT_BODY_ORDER` (opcional; por defecto `giver_name,receiver_name,budget,deadline,note,admin_name`)
- `KAPSO_TEMPLATE_ADMIN_BODY_ORDER` (opcional; por defecto `code`)
- `KAPSO_TEMPLATE_ADMIN_BUTTON_INDEX` (opcional; si tu template de admin tiene boton URL dinamico, ej `0`)
- `KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE_SOURCE=draw_link|code|env` (opcional; por defecto `draw_link`)
- `KAPSO_TEMPLATE_ADMIN_BUTTON_VALUE` (opcional; usado cuando `...VALUE_SOURCE=env`)

## Notas de datos

- El contacto del administrador se modela como `admin_contact` (alias legacy en codigo: `email_admin`).
- En `VERCEL_ENV=production`, la app falla al iniciar si faltan `DATABASE_URL/POSTGRES_*` (no usa fallback SQLite).

## Expiracion de sorteos (diseno futuro)

Hoy `fecha_expiracion` se guarda pero no se aplica como bloqueo funcional. Para convertirla en una regla real, este es el plan recomendado:

1. Definir politica de negocio
- Regla base: si el usuario no define fecha limite (`meta.deadline`), el sorteo expira a `fecha_creacion + 7 dias`.
- Si define `deadline`, decidir una de estas politicas:
  - `strict_deadline`: expira al final del dia del `deadline` (timezone de negocio).
  - `max_window`: expira en `min(deadline, fecha_creacion + N dias)` para evitar links eternos.
- Mantener la fecha en UTC en DB y convertir solo para mostrar.

2. Calcular `fecha_expiracion` al crear sorteo
- Implementar un helper dedicado (ej: `compute_draw_expiration(meta_deadline, created_at)`).
- Parsear `deadline` con formato soportado por frontend (`dd/mm` o `dd/mm/aaaa`).
- Si el parseo falla, usar fallback a `+7 dias` y loguear warning tecnico.

3. Aplicar expiracion en lectura y operaciones
- En `load_draw_data(code)`, verificar `now_utc > sorteo.fecha_expiracion`:
  - API: responder `404` o `410` con codigo de error semantico (`DRAW_EXPIRED`).
  - Vista `/sorteo/<code>`: mostrar estado "Sorteo expirado" sin datos sensibles.
- En endpoints de accion:
  - `POST /api/sorteo/<code>/resend`: bloquear si expiro.
  - `PATCH /api/sorteo/<code>/participant/<id>/contact`: bloquear si expiro.

4. Contrato de API y frontend
- Agregar campos de estado en `GET /api/sorteo/<code>`:
  - `expired: bool`
  - `expires_at: iso8601`
  - `can_resend: bool`
  - `can_edit_contacts: bool`
- En UI admin, deshabilitar botones cuando este expirado y mostrar mensaje explicito.

5. Migracion y compatibilidad
- DB ya tiene `fecha_expiracion`; completar registros legacy que esten `NULL` con backfill (`fecha_creacion + 7 dias`).
- Evitar cambios destructivos de esquema; usar migracion incremental.
- Durante rollout, soportar ambos comportamientos detras de flag (`ENFORCE_DRAW_EXPIRATION=0|1`).

6. Observabilidad y operaciones
- Log estructurado para bloqueos por expiracion (`error_code=DRAW_EXPIRED`, `draw_code`, `expires_at`).
- Metricas sugeridas:
  - cantidad de accesos a sorteos expirados,
  - reenvios bloqueados por expiracion,
  - contactos bloqueados por expiracion.

7. Limpieza de datos (opcional)
- Tarea programada para archivar o eliminar sorteos expirados + antiguos (ej: > 90 dias).
- Si se elimina, respetar cascada de `participante`, `asignacion` y `email_envio`.

