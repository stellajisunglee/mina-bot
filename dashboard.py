"""Read-only admin analytics dashboard. Single-password HTTP Basic Auth, no
user accounts. Meant to be reached via SSH tunnel -- uvicorn should bind to
127.0.0.1 only; Basic Auth alone isn't safe to expose on the open internet.

Never writes anything. Only reads metrics.jsonl (via storage.py) and
self_declared_levels.json (via dashboard_data.py).
"""
import os
import secrets

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

import dashboard_data

load_dotenv()

DASHBOARD_USERNAME = "admin"
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
assert DASHBOARD_PASSWORD, "DASHBOARD_PASSWORD must be set in .env"

app = FastAPI()
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    valid_username = secrets.compare_digest(credentials.username, DASHBOARD_USERNAME)
    valid_password = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/")
def dashboard(request: Request, _: None = Depends(require_auth)):
    data = dashboard_data.build_dashboard_data()
    return templates.TemplateResponse(request, "dashboard.html", {"data": data})
