<div align="center">

  <img src="static/sorty_logo.png" alt="Sorty Logo" width="140" />

  <h1>Sorty</h1>

  <p>
    Sorty es una app web para organizar sorteos de amigo invisible con un flujo simple: cargas participantes, defines exclusiones, generas asignaciones validas y envias notificaciones por email o WhatsApp.
  </p>

</div>

## Estado actual

- `https://sorty.com.ar/` muestra la landing (marketing + SEO).
- `https://sorty.com.ar/app` muestra la app operativa para crear el sorteo.
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


