"""Nutmeg entry point.

    python main.py

All application code lives under src/; this is just the launcher, so it's
the one obvious place to look for "how do I run this." For local development
with autoreload, use uvicorn directly instead -- reload needs an import-string
target, not an app object, so it can't be driven from here:

    uvicorn src.api.server:app --reload
"""

import uvicorn

from src.api.server import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
