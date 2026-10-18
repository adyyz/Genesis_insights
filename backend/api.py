"""Current corpus APIs; private operations require verified Firebase tokens."""
from pathlib import Path
from typing import Annotated
import json
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen

import firebase_admin
from firebase_admin import auth
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from google.api_core.exceptions import GoogleAPICallError

from .config import Settings
from .bulk_dataset import ID as CORPUS_ID
from .models import DailyControlRequest, FeedbackRequest
from .usage import run_summary
from .repository import Conflict, Repository

ROLES = [
    {"id": 1, "name": "Evidence enrichment", "kind": "Deterministic", "output": "Transaction trail, facts, missing context", "used_by": "Risk assessment and investigation"},
    {"id": 2, "name": "Risk assessment", "kind": "Code + Gemini", "output": "Five scores and evidence-linked explanation", "used_by": "Categorization and analyst"},
    {"id": 3, "name": "Typology categorization", "kind": "Gemini", "output": "Typology and suggested action", "used_by": "Investigation brief and prioritization"},
    {"id": 4, "name": "Investigation brief", "kind": "Gemini", "output": "Findings, counter-evidence, next steps", "used_by": "Human analyst review"},
    {"id": 0, "name": "Machine pre-label and priority", "kind": "Deterministic", "output": "Versioned heuristic pre-label and review priority", "used_by": "Analyst queue"},
    {"id": 5, "name": "Evolution intelligence", "kind": "Statistics + Gemini", "output": "Versioned improvement proposals and evaluation", "used_by": "Monitoring lead; approval required"},
]

def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()
    repo = Repository(settings, CORPUS_ID)
    app = FastAPI(title="Genesis", version="0.1.0")
    firebase_app = firebase_admin.initialize_app(options={"projectId": settings.project_id}, name=f"api-{id(app)}")
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def private_cache_policy(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == '/api/config' or request.url.path.startswith(("/api/operator/", "/api/admin/", "/api/demo/bulk/", "/api/demo/usage")):
            response.headers["Cache-Control"] = "no-store"
            response.headers.append("Vary", "Authorization")
        return response

    @app.exception_handler(Conflict)
    async def conflict_error(_request: Request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(GoogleAPICallError)
    async def database_error(_request: Request, _exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": "Database unavailable. Start the local emulators and seed the dataset."})

    def operator(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if not credentials: raise HTTPException(401, "Operator sign-in required")
        try:
            claims = auth.verify_id_token(credentials.credentials, app=firebase_app, check_revoked=True)
        except (ValueError, auth.InvalidIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError):
            raise HTTPException(401, "Invalid or expired operator token")
        if claims["uid"] not in settings.operator_uids: raise HTTPException(403, "Operator account is not allowlisted")
        return claims["uid"]

    def admin(actor=Depends(operator)):
        if actor not in settings.admin_uids: raise HTTPException(403, "Admin account is not allowlisted")
        return actor

    @app.get("/api/demo/usage")
    def public_usage():
        from .baseline_workspace import accounting
        return accounting(repo)

    @app.get("/api/operator/usage")
    def operator_usage(_actor=Depends(operator), scope: str = Query("corpus", pattern="^corpus$")):
        from .baseline_workspace import accounting
        return accounting(repo, operator_summary=True)

    @app.get("/api/operator/runs")
    def runs(_actor=Depends(operator), scope: str = Query("corpus", pattern="^corpus$"), limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, pattern=r"^[A-Za-z0-9_-]{1,128}$")):
        from .baseline_workspace import selected_id
        page = repo.page("pipeline_runs", limit, cursor, [("dataset_id", selected_id(repo) or CORPUS_ID)], order="created_at")
        return {**page, "items": [run_summary(run) for run in page["items"]]}

    @app.get("/api/operator/runs/{run_id}")
    def run(run_id: str, actor=Depends(operator)):
        try:
            from .bulk_jobs import scoped
            worker = scoped(repo, repo.get("pipeline_runs", run_id))
            return {**worker.run_detail(run_id), "initiated_by_me": worker.get("pipeline_runs", run_id).get("actor_uid") == actor}
        except LookupError as exc: raise HTTPException(404, str(exc))

    @app.put("/api/admin/usage-daily")
    def daily_control(request: DailyControlRequest, actor=Depends(admin)):
        return repo.update_daily_control(request, actor)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": settings.mode, "live_ai_enabled": settings.live_ai,
            "dataset": "synthetic", "cloud_readiness": "UNVERIFIED"}

    @app.get("/api/config")
    def config():
        value = {"mode": settings.mode, "project_id": settings.project_id,
            "auth_emulator_url": "http://" + settings.auth_host if settings.mode == "local" else None,
            "firebase_web_api_key": settings.firebase_web_api_key if settings.mode == "cloud" else None,
            "live_ai_enabled": settings.live_ai}
        credentials = settings.operator_demo_credentials()
        if (settings.mode == 'cloud' and settings.operator_demo_login_enabled and credentials
            and credentials['uid'] in settings.operator_uids and credentials['uid'] in settings.admin_uids):
            value['operator_demo_email'] = credentials['email']
        return value

    @app.post('/api/operator/demo-login')
    def demo_login():
        # Enable only after verifying private Cloud Run IAM; disable before public release.
        credentials = settings.operator_demo_credentials()
        if settings.mode != 'cloud' or not settings.operator_demo_login_enabled or not credentials:
            raise HTTPException(404, 'Stored operator login is unavailable')
        uid = credentials['uid']
        if uid not in settings.operator_uids or uid not in settings.admin_uids:
            raise HTTPException(403, 'Stored operator is not allowlisted')
        if not settings.firebase_web_api_key: raise HTTPException(503, 'Operator sign-in is not configured')
        try:
            url = 'https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?' + urlencode({'key': settings.firebase_web_api_key})
            payload = json.dumps({'email': credentials['email'], 'password': credentials['password'], 'returnSecureToken': True}).encode()
            request = URLRequest(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(request, timeout=10) as response:
                raw = response.read(65537)
            if len(raw) > 65536: raise ValueError()
            result = json.loads(raw)
            token = result.get('idToken')
            if not isinstance(token, str) or not token or result.get('localId') != uid: raise ValueError()
            claims = auth.verify_id_token(token, app=firebase_app, check_revoked=True)
            if claims.get('uid') != uid: raise ValueError()
        except Exception:
            # Transport failures may contain request data; expose no provider detail.
            raise HTTPException(502, 'Stored operator sign-in failed') from None
        return {'idToken': token}

    @app.get("/api/system/agents")
    def agents():
        return {"roles": ROLES, "automatic_activation": False, "model_execution_status": "NOT_CONNECTED"}

    @app.get("/api/operator/session")
    def session(actor=Depends(operator)):
        return {"uid": actor, "mode": settings.mode, "is_admin": actor in settings.admin_uids, "live_ai_eligible": settings.ai_authorized()}

    @app.get("/api/operator/audit")
    def audit(_actor=Depends(operator), scope: str = Query("corpus", pattern="^corpus$"), limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, pattern=r"^[A-Za-z0-9_-]{1,128}$")):
        from .baseline_workspace import selected_id
        filters = [("dataset_id", selected_id(repo) or CORPUS_ID)]
        return repo.page("audit_events", limit, cursor, filters, order="occurred_at")

    def alert_repo(alert_id):
        from .bulk_jobs import scoped
        return scoped(repo, repo.get("alerts", alert_id))

    @app.post("/api/operator/alerts/{alert_id}/feedback")
    def feedback(alert_id: str, request: FeedbackRequest, actor=Depends(operator)):
        try: return alert_repo(alert_id).save_feedback(alert_id, request, actor)
        except LookupError as exc: raise HTTPException(404, str(exc))
        except Conflict as exc: raise HTTPException(409, str(exc))

    @app.get("/api/operator/alerts/{alert_id}")
    def private_alert(alert_id: str, _actor=Depends(operator)):
        try: return alert_repo(alert_id).alert_detail(alert_id)
        except LookupError as exc: raise HTTPException(404, str(exc))
        except Conflict as exc: raise HTTPException(409, str(exc))

    @app.get("/api/operator/alerts/{alert_id}/feedback")
    def feedback_history(alert_id: str, _actor=Depends(operator), limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, pattern=r"^[A-Za-z0-9_-]{1,128}$")):
        alert = repo.get("alerts", alert_id)
        if not alert: raise HTTPException(404, "Alert not found")
        try:
            worker = alert_repo(alert_id)
            worker.operational_case(alert["case_id"])
        except LookupError as exc: raise HTTPException(404, str(exc))
        return worker.page("feedback_events", limit, cursor, [("alert_id", alert_id), ("dataset_id", worker.dataset_id)], order="revision")

    from .bulk_api import register as register_bulk
    register_bulk(app, repo, operator)
    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def missing_api(path: str):
        raise HTTPException(404, "API route not found")

    dist = Path(__file__).resolve().parents[1] / "dist"
    if dist.exists(): app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app

app = create_app()
