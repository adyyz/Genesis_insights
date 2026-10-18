"""Explicit one-request connectivity check; no triage or human feedback is implied."""
from dataclasses import replace
from datetime import timedelta
import json
from typing import Literal

from google.api_core.exceptions import AlreadyExists
from pydantic import BaseModel, ConfigDict

from .config import Settings
from .dataset import DATASET_ID, content_hash, stable_id
from .gateway import generate_json
from .models import DailyControlRequest, PipelineRun, StepRun
from .repository import Repository, now


class SmokeReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["OK"]


def smoke():
    local = Settings.load()
    if local.mode != "local": raise RuntimeError("Smoke test requires local emulators")
    settings = replace(local, local_gemini_enabled=True, local_gemini_free_verified=True,
        local_gemini_verified_until=now() + timedelta(minutes=5))
    if not settings.local_gemini_valid(): raise RuntimeError("Secret key or confirmed local free-tier configuration missing")
    repo = Repository(settings)
    prompt = 'Connectivity check only. Return JSON with the single field status set to OK. No explanation.'
    run_id = stable_id(DATASET_ID, "developer-api-smoke-v1", settings.active_model_id)
    existing = repo.get("pipeline_runs", run_id)
    if existing:
        print(json.dumps({"run_id": run_id, "state": existing["state"], "reused": True}))
        return 0 if existing["state"] == "COMPLETE" else 1
    bundle = {"kind": "BUNDLE", "origin": "BASELINE", "body": {"purpose": "DEVELOPER_API_CONNECTIVITY_ONLY",
        "model": settings.active_model_id, "prompt": prompt, "schema": SmokeReply.model_json_schema(),
        "input_cap": 512, "output_cap": 128, "max_generation_attempts": 1, "automatic_retries": 0}}
    bundle_id = content_hash(bundle)
    try: repo.create("config_versions", bundle_id, {**bundle, "content_hash": bundle_id})
    except AlreadyExists: pass
    repo.create("pipeline_runs", run_id, PipelineRun(id=run_id, dataset_id=DATASET_ID, kind="EVALUATION",
        state="RUNNING", config_bundle_id=bundle_id, actor_uid="local-smoke-operator", started_at=now()).model_dump())
    step = StepRun(id=stable_id(run_id, "EVALUATE", 1), dataset_id=DATASET_ID, run_id=run_id, leg_id="EVALUATE",
        config_bundle_id=bundle_id, state="STARTED", started_at=now(), input_hash=content_hash(prompt),
        model_id=settings.active_model_id, usage_status="UNKNOWN", cost_status="UNKNOWN")
    try:
        daily = repo.daily_control()
        if daily["attempts_reserved"]: raise RuntimeError("Daily attempts already reserved; refusing another smoke request")
        repo.update_daily_control(DailyControlRequest(expected_revision=daily["revision"], live_enabled=True, limit=1), "local-smoke-operator")
        result = generate_json(repo, step, prompt, SmokeReply, input_limit=512, output_limit=128)
        state = "COMPLETE" if result.outcome == "SUCCEEDED" else "FAILED"
        repo.db.collection("pipeline_runs").document(run_id).update({"state": state, "completed_at": now(), "error_code": result.error_code})
        detail = repo.run_detail(run_id)
        print(json.dumps({"run_id": run_id, "step_id": result.step_id, "state": state, "error_code": result.error_code,
            "model": settings.active_model_id, "usage": detail["usage"]}))
        return 0 if state == "COMPLETE" else 1
    finally:
        daily = repo.daily_control()
        repo.update_daily_control(DailyControlRequest(expected_revision=daily["revision"], live_enabled=False,
            limit=max(1, daily["limit"])), "local-smoke-operator")


if __name__ == "__main__":
    raise SystemExit(smoke())
