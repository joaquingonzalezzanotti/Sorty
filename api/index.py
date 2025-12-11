from app import app

# Expose WSGI application for Vercel Python runtime.
handler = app
application = app
