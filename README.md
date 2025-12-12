# Sorty

Sorty es mi herramienta personal para organizar sorteos de amigo secreto sin enredos: cargas participantes, marcas exclusiones y el sistema se encarga de asignar y enviar los correos.

## Características
- Interfaz web en español con modo claro/oscuro y flujo guiado por secciones.
- Validación de participantes, exclusiones y admin único antes de sortear.
- Envío de correos HTML y texto plano para cada participante y un resumen para el administrador.
- Modo "console" para simular envíos durante el desarrollo y modo SMTP listo para producción.

## Requisitos
- Python 3.10+
- Flask (instalación en `requirements.txt`)

## Ejecución local
1. Crear y activar entorno virtual:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Configurar variables de entorno mínimas (ejemplo con modo consola):
   ```bash
   export EMAIL_MODE=console
   export SMTP_FROM_NAME="Sorty"
   export SMTP_FROM_EMAIL="sorty@example.com"
   ```
   Para enviar correos reales agrega `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER` y `SMTP_PASS`.
3. Iniciar la app:
   ```bash
   python app.py
   ```
4. Abrir http://localhost:5000 y cargar participantes. Con el modo consola verás las simulaciones en la terminal.

## Cómo funciona el sorteo
- El formulario exige nombre, email y un único administrador; las exclusiones se validan contra la lista de participantes.
- El servidor busca asignaciones válidas respetando exclusiones y evita autoasignaciones. Si no hay solución, responde con un error amigable.
- Si `send` es verdadero en `/api/draw`, el backend arma los mensajes y los envía según el modo configurado.

## Nota personal
Este proyecto nació para organizar los intercambios de regalos de amigos y familia sin depender de hojas de cálculo. Si encuentras útil la herramienta o tienes ideas de mejora, ¡me encantará saberlo!
