"""Entry point: `flask --app app run` / `gunicorn app:app`."""
from rwc import create_app

app = create_app()
