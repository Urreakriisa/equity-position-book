"""Entry point for uvicorn: `uvicorn app.server:app`."""
from .main import create_app

app = create_app()
