from app import app
from vercel_serverless_wsgi import handle_wsgi


def handler(event, context):
  """Entrypoint para Vercel (Serverless Function)."""
  return handle_wsgi(app, event, context)


# Alias util por si alguna herramienta espera "app" como callable.
application = app
