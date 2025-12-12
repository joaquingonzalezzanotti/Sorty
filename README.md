# Sorty

Sorty es mi herramienta personal para organizar sorteos de amigo secreto sin enredos: cargas participantes, marcas exclusiones y el sistema se encarga de asignar y enviar los correos.

## Características
- Interfaz web en español con modo claro/oscuro y flujo guiado por secciones.
- Validación de participantes, exclusiones y administrador único antes de sortear.
- Envío de correos HTML y texto plano para cada participante y un resumen para el administrador.

## Requisitos
- Python 3.10+
- Flask (instalación en `requirements.txt`)

## Ejecución web
- No necesitas instalar nada para usar la herramienta. Puedes acceder a la versión desplegada directamente en:
  https://sorty-neon.vercel.app/


## Cómo funciona el sorteo
- El formulario exige nombre, email y un único administrador; las exclusiones se validan contra la lista de participantes.
- El servidor busca asignaciones válidas respetando exclusiones y evita autoasignaciones. Si no hay solución, responde con un error amigable.
- Si `send` es verdadero en `/api/draw`, el backend arma los mensajes y los envía según el modo configurado.

## Nota personal
Este proyecto nació para organizar los intercambios de regalos de amigos y familia sin depender de hojas de cálculo. Si encuentras útil la herramienta o tienes ideas de mejora, ¡me encantará saberlo!
