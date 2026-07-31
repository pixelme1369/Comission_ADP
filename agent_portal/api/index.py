"""Vercel Python runtime entrypoint. Vercel imports this module and looks for
a WSGI-callable named `app` — Flask's app object satisfies that directly."""

import sys
from pathlib import Path

# So `import agent_portal` resolves regardless of Vercel's working directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_portal import create_app, db

app = create_app()

with app.app_context():
    db.create_all()
