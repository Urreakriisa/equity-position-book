"""Run the app offline with stand-in market data:  python -m tests.run_local [csv_dir]"""
import os
import sys

import uvicorn

from app.main import create_app
from app.store import Store
from tests.fake_av import FakeAV

os.environ.setdefault("APP_PASSWORD", "local")
os.environ.setdefault("SECRET_KEY", "local-dev")
av = FakeAV.from_csv_dir(sys.argv[1]) if len(sys.argv) > 1 else FakeAV()
app = create_app(Store(os.environ.get("LOCAL_DB", "sqlite:///local-test.db")), av=av)
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")), log_level="warning")
