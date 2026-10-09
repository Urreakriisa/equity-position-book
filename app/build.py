"""Build number shown in the app. Increase BUILD by one in every change pushed to main."""
import os
import time

BUILD = 12
COMMIT = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:7]
STARTED = time.time()


def info() -> dict:
    return {"number": BUILD, "commit": COMMIT, "started": int(STARTED)}
