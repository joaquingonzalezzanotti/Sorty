# Sorty

Sorty es una app web para organizar sorteos de amigo invisible con un flujo simple: cargas participantes, defines exclusiones, generas asignaciones validas y envias correos.

## Estado actual

- `https://sorty-neon.vercel.app/` muestra la landing (marketing + SEO).
- `https://sorty-neon.vercel.app/app` muestra la app operativa para crear el sorteo.
- Cada sorteo guardado tiene una vista de administracion en `/sorteo/<code>` (alias legado: `/draw/<code>`).

## Funcionalidades principales

- Landing en `/` con metadata SEO (title, description, Open Graph, Twitter y JSON-LD).
- Formulario en `/app` con:
  - participantes (nombre + email),
  - administrador unico,
  - exclusiones personalizadas,
  - presupuesto, fecha limite y nota grupal.
- Validaciones de negocio:
  - minimo 3 participantes,
  - emails validos y sin duplicados,
  - 1 solo administrador,
  - sin autoasignacion,
  - control de restricciones imposibles.
- Generacion de asignaciones con backtracking.
- Persistencia de sorteos en base de datos (codigo publico y UUID).
- Envio de correo individual a cada participante + correo resumen para el admin.
- Vista admin de sorteo con:
  - copiar link,
  - reenviar correos,
  - corregir email de participante,
  - opcion de notificar al correo anterior.

## Stack

- Python + Flask
- Flask-SQLAlchemy
- PostgreSQL (Neon/Vercel) o SQLite local como fallback
- Frontend server-rendered (Jinja + CSS + JS vanilla)
- Deploy en Vercel (`api/index.py` expone la app WSGI)

## Variables de entorno

Base de datos:

- `DATABASE_URL` (prioridad alta)
- `POSTGRES_PRISMA_URL`
- `POSTGRES_URL`
- `POSTGRES_URL_NON_POOLING`

Email:

- `EMAIL_MODE`: `smtp` (default) o `console`
- `SMTP_HOST` (default: `smtp.gmail.com`)
- `SMTP_PORT` (default: `587`)
- `SMTP_USER`
- `SMTP_PASS`
- `SMTP_FROM_EMAIL` (opcional, usa `SMTP_USER` si no existe)
- `SMTP_FROM_NAME` (opcional, default: `Sorty`)

URL publica:

- `PUBLIC_APP_URL` (se usa para links absolutos y assets en emails)

## API principal

- `POST /api/sorteo` (alias: `POST /api/draw`): crea sorteo, guarda datos y opcionalmente envia correos.
- `GET /api/sorteo/<code>` (alias: `GET /api/draw/<code>`): obtiene datos del sorteo.
- `POST /api/sorteo/<code>/resend` (alias: `POST /api/draw/<code>/resend`): reenvia correos.
- `PATCH /api/sorteo/<code>/participant/<id>/email` (alias `.../draw/...`): corrige email de participante.

## Tests UI

Hay pruebas de Playwright en `tests/ui.spec.js` y scripts npm en `package.json`.
