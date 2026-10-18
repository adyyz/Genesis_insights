import os
import re
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

LOCAL_FREE_MODELS = frozenset({"gemini-3.5-flash-lite", "gemini-3-flash-preview"})


@dataclass(frozen=True)
class Settings:
    mode: str
    project_id: str
    firestore_host: str | None
    auth_host: str | None
    live_ai: bool
    model_id: str
    operator_uids: frozenset[str]
    admin_uids: frozenset[str] = frozenset()
    credits_verified: bool = False
    credit_verified_until: datetime | None = None
    local_gemini_enabled: bool = False
    local_gemini_free_verified: bool = False
    local_gemini_verified_until: datetime | None = None
    local_gemini_model: str = "gemini-3.5-flash-lite"
    worker_audience: str = ""
    task_invoker_email: str = ""
    cloud_tasks_enabled: bool = False
    task_queue_path: str = ""
    verified_cloud_model_id: str = ""
    verified_cloud_location: str = ""
    firebase_web_api_key: str = ""
    verified_rate_card_json: str = ""
    deployed_code_fingerprint: str = ""
    agent_profiles_json: str = ""
    operator_demo_login: str = field(default="", repr=False)
    operator_demo_login_enabled: bool = False
    verified_cloud_leg_models_json: str = ""

    def operator_demo_credentials(self):
        if self.mode != 'cloud' and (self.operator_demo_login or self.operator_demo_login_enabled):
            raise ValueError('Stored operator login requires private cloud staging')
        if not self.operator_demo_login: return None
        try:
            value = json.loads(self.operator_demo_login)
            if (not isinstance(value, dict) or set(value) != {'email', 'password', 'uid'}
                or any(not isinstance(item, str) or not item.strip() for item in value.values())):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('Invalid stored operator login configuration') from None
        return value

    def worker_configured(self):
        try:
            audience = urlparse(self.worker_audience)
            return (self.mode == "cloud" and audience.scheme == "https" and bool(audience.hostname)
                and not audience.username and not audience.password and not audience.query and not audience.fragment
                and audience.path in {"", "/"} and audience.port in {None, 443}
                and self.task_invoker_email.endswith("@" + self.project_id + ".iam.gserviceaccount.com")
                and bool(self.task_invoker_email.split("@", 1)[0]))
        except ValueError: return False

    def local_gemini_valid(self):
        return (self.mode == "local" and self.project_id == "demo-aml-insight" and self.local_gemini_enabled
            and self.local_gemini_free_verified and self.local_gemini_model in LOCAL_FREE_MODELS
            and bool(os.getenv("GEMINI_API_KEY")) and self.local_gemini_verified_until is not None
            and self.local_gemini_verified_until > datetime.now(timezone.utc))

    def ai_authorized(self):
        try:
            self.agent_profiles
            self.verified_cloud_leg_models
        except ValueError: return False
        return self.local_gemini_valid() or self.cloud_model_valid()

    @property
    def verified_cloud_leg_models(self):
        if not self.verified_cloud_leg_models_json:
            return dict.fromkeys(("A2", "A3", "A4", "A5"), self.verified_cloud_model_id)
        try:
            models = json.loads(self.verified_cloud_leg_models_json)
            if (not isinstance(models, dict) or set(models) != {"A2", "A3", "A4", "A5"}
                or any(not isinstance(model, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,127}", model) for model in models.values())):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("Invalid verified cloud leg models configuration") from None
        return models

    def cloud_model_valid(self):
        return (self.credits_valid() and not self.local_gemini_enabled and self.verified_cloud_model_id == self.model_id and bool(self.model_id)
            and bool(re.fullmatch(r"global|[a-z]+(?:-[a-z]+)+[0-9]+", self.verified_cloud_location)))

    def is_model_authorized(self, model_id: str, leg_id: str | None = None) -> bool:
        if self.mode == "local":
            return model_id == self.active_model_id and (not self.local_gemini_enabled or model_id in LOCAL_FREE_MODELS)
        expected = self.verified_cloud_model_id if leg_id in {None, "EVALUATE"} else self.verified_cloud_leg_models.get(leg_id)
        return (bool(model_id) and model_id == expected and self.model_id == self.verified_cloud_model_id
            and bool(re.fullmatch(r"global|[a-z]+(?:-[a-z]+)+[0-9]+", self.verified_cloud_location)))

    @property
    def agent_profiles(self):
        from .agent_profiles import load_agent_profiles
        return load_agent_profiles(self.agent_profiles_json, fallback_model=self.active_model_id)

    @property
    def active_model_id(self):
        return self.local_gemini_model if self.local_gemini_enabled else self.model_id

    @property
    def ai_verified_until(self):
        return self.local_gemini_verified_until if self.local_gemini_enabled else self.credit_verified_until

    def credits_valid(self):
        return (self.mode == "cloud" and self.live_ai and self.credits_verified
            and self.credit_verified_until is not None and self.credit_verified_until > datetime.now(timezone.utc))

    @classmethod
    def load(cls):
        mode = os.getenv("APP_MODE", "local")
        if mode not in {"local", "cloud"}: raise ValueError("APP_MODE must be local or cloud")
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "demo-aml-insight" if mode == "local" else "")
        firestore = os.getenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8081" if mode == "local" else "") or None
        auth = os.getenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099" if mode == "local" else "") or None
        if mode == "local":
            if project != "demo-aml-insight": raise ValueError("Local mode requires the demo-aml-insight project")
            for host in [firestore, auth]:
                if not host or urlparse("http://" + host).hostname not in {"localhost", "127.0.0.1"}:
                    raise ValueError("Local emulators must use loopback hosts")
            os.environ["FIRESTORE_EMULATOR_HOST"] = firestore
            os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = auth
        elif not project or project.startswith("demo-") or firestore or auth:
            raise ValueError("Cloud mode requires a real project and forbids emulator settings")
        uids = frozenset(filter(None, os.getenv("OPERATOR_UIDS", "operator-demo" if mode == "local" else "").split(",")))
        live_ai = os.getenv("LIVE_AI_ENABLED", "false").lower() == "true"
        if live_ai and (mode != "cloud" or os.getenv("CREDITS_VERIFIED", "false").lower() != "true"):
            raise ValueError("Live AI requires cloud mode and explicit verified credit coverage")
        admins = frozenset(uid.strip() for uid in os.getenv("ADMIN_UIDS", "operator-demo" if mode == "local" else "").split(",") if uid.strip())
        expiry_text = os.getenv("CREDIT_VERIFIED_UNTIL", "")
        expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00")) if expiry_text else None
        if expiry is not None and expiry.tzinfo is None: raise ValueError("CREDIT_VERIFIED_UNTIL requires a timezone")
        local_enabled = os.getenv("LOCAL_GEMINI_ENABLED", "false").lower() == "true"
        if local_enabled and mode != "local": raise ValueError("LOCAL_GEMINI_ENABLED requires local emulators")
        local_expiry_text = os.getenv("LOCAL_GEMINI_VERIFIED_UNTIL", "")
        local_expiry = datetime.fromisoformat(local_expiry_text.replace("Z", "+00:00")) if local_expiry_text else None
        if local_expiry is not None and local_expiry.tzinfo is None: raise ValueError("LOCAL_GEMINI_VERIFIED_UNTIL requires a timezone")
        source_fingerprint = os.getenv("DEPLOYED_CODE_FINGERPRINT", "")
        if source_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint):
            raise ValueError("DEPLOYED_CODE_FINGERPRINT requires the verified SHA256 source fingerprint")
        settings = cls(mode, project, firestore, auth, live_ai, os.getenv("GEMINI_MODEL", "gemini-3.5-flash"), uids,
            admins, os.getenv("CREDITS_VERIFIED", "false").lower() == "true", expiry,
            local_enabled, os.getenv("LOCAL_GEMINI_FREE_VERIFIED", "false").lower() == "true", local_expiry,
            worker_audience=os.getenv("WORKER_AUDIENCE", ""), task_invoker_email=os.getenv("TASK_INVOKER_EMAIL", ""),
            cloud_tasks_enabled=os.getenv("CLOUD_TASKS_ENABLED", "false").lower() == "true",
            task_queue_path=os.getenv("TASK_QUEUE_PATH", ""),
            verified_cloud_model_id=os.getenv("VERIFIED_CLOUD_MODEL_ID", ""),
            verified_cloud_location=os.getenv("VERIFIED_CLOUD_LOCATION", ""),
            firebase_web_api_key=os.getenv("FIREBASE_WEB_API_KEY", "").strip(),
            verified_rate_card_json=os.getenv("VERIFIED_RATE_CARD_JSON", ""), deployed_code_fingerprint=source_fingerprint,
            agent_profiles_json=os.getenv("AGENT_PROFILES_JSON", ""),
            local_gemini_model=os.getenv("LOCAL_GEMINI_MODEL", "gemini-3.5-flash-lite"),
            operator_demo_login=os.getenv('OPERATOR_DEMO_LOGIN', ''),
            operator_demo_login_enabled=os.getenv('OPERATOR_DEMO_LOGIN_ENABLED', 'false').lower() == 'true',
            verified_cloud_leg_models_json=os.getenv('VERIFIED_CLOUD_LEG_MODELS_JSON', ''))
        settings.agent_profiles  # Explicit invalid configuration must not silently select defaults.
        settings.verified_cloud_leg_models
        settings.operator_demo_credentials()
        return settings
