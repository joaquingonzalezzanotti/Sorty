from app import app
from vercel_wsgi import handle_request


def handler(request, context):
  """Entrypoint para Vercel (Serverless Function)."""
  return handle_request(app, request, context)


# Alias util si Vercel expone por nombre app/application.
application = app
