"""The only model transport boundary. No SDK is loaded on the refusing path."""
from dataclasses import dataclass
import json
import os
import re
from time import monotonic, sleep

from pydantic import BaseModel, ValidationError

_last_local_http_at = None


def _pace_local(settings):
    """One sequential local worker; cloud dispatch retains its own rate controls."""
    global _last_local_http_at
    if settings.mode != "local": return
    interval = float(os.getenv("LOCAL_GEMINI_MIN_INTERVAL_SECONDS", "0"))
    if not 0 <= interval <= 60: raise ValueError("Invalid local provider interval")
    if not interval: return
    if _last_local_http_at is not None:
        delay = interval - (monotonic() - _last_local_http_at)
        if delay > 0: sleep(delay)
    _last_local_http_at = monotonic()


@dataclass(frozen=True)
class GatewayResult:
    outcome: str = "NOT_SENT"
    error_code: str | None = "AI_NOT_CONNECTED"
    output: dict | None = None
    step_id: str | None = None
    context_precount_tokens: int | None = None


def call_model(settings, **_request) -> GatewayResult:
    # Compatibility refusal probe; real opt-in work uses generate_json in workers.
    return GatewayResult()


def _provider_client(settings):
    if not settings.ai_authorized():
        from .repository import Conflict
        raise Conflict("AI_NOT_CONNECTED")
    from google import genai
    from google.genai import types
    if settings.mode == "cloud":
        return genai.Client(enterprise=True, project=settings.project_id, location=settings.verified_cloud_location,
            http_options=types.HttpOptions(api_version="v1", timeout=30000, retry_options=types.HttpRetryOptions(attempts=1)))
    return genai.Client(enterprise=False, api_key=os.environ["GEMINI_API_KEY"].strip(),
        http_options=types.HttpOptions(base_url="https://generativelanguage.googleapis.com", api_version="v1beta",
            timeout=30000, retry_options=types.HttpRetryOptions(attempts=1)))


def _retry_delay(exc):
    if getattr(exc, "code", None) != 429 or getattr(exc, "status", None) != "RESOURCE_EXHAUSTED": return None
    body = getattr(exc, "details", None)
    if not isinstance(body, dict): return None
    error = body.get("error", body)
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, list) or len(details) != 1 or not isinstance(details[0], dict): return None
    # Only explicit short RetryInfo, with no quota/billing/other failure details.
    info = details[0]
    delay = info.get("retryDelay")
    if info.get("@type") != "type.googleapis.com/google.rpc.RetryInfo" or not isinstance(delay, str): return None
    if not re.fullmatch(r"[0-5](?:\.[0-9]{1,9})?s", delay): return None
    seconds = float(delay[:-1])
    return max(1, seconds) if seconds <= 5 else None


def _count_config(settings, model_id, contents, schema):
    """Count the native response schema as well as the exact prompt."""
    generation = {"response_mime_type": "application/json", "response_json_schema": schema}
    if settings.mode == "cloud": return {"generation_config": generation}
    # Developer API supports GenerateContentRequest on countTokens; the SDK's
    # generation_config shortcut currently supports only the cloud transport.
    return {"http_options": {"extra_body": {"contents": None, "generateContentRequest": {
        "model": "models/" + model_id, "contents": [{"role": "user", "parts": [{"text": contents}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema}}}}}


def generate_json(repo, step, prompt: str, schema: type[BaseModel], *, input_limit: int, output_limit: int,
                  evidence: dict | None = None, model_id: str | None = None,
                  thinking_level: str | None = None, temperature: float | None = None,
                  structured_output: bool | None = None):
    """One explicitly authorized provider attempt; no SDK retries or fallback."""
    from .repository import Conflict, now
    from .dataset import content_hash
    if not repo.settings.ai_authorized(): return GatewayResult()
    target_model = model_id or step.model_id or repo.settings.active_model_id
    if repo.settings.mode == "cloud" and not step.worker_lease: return GatewayResult(error_code="WORKER_LEASE_REQUIRED")
    if repo.settings.mode == "cloud":
        bundle = repo.get("config_versions", step.config_bundle_id) or {}
        if (not re.fullmatch(r"[0-9a-f]{64}", repo.settings.deployed_code_fingerprint)
            or bundle.get("body", {}).get("code_fingerprint") != repo.settings.deployed_code_fingerprint):
            return GatewayResult(error_code="CONFIG_SOURCE_MISMATCH")
    if not repo.settings.is_model_authorized(target_model, step.leg_id):
        return GatewayResult(error_code="MODEL_NOT_VERIFIED")
    if step.model_id is not None and step.model_id != target_model:
        return GatewayResult(error_code="CONFIG_MODEL_MISMATCH")
    if step.leg_id in {"A2", "A3", "A4", "A5"} and evidence is None: return GatewayResult(error_code="EVIDENCE_REQUIRED")
    if not repo.daily_control()["live_enabled"]: return GatewayResult(error_code="LIVE_AI_DISABLED")
    bundle = repo.get("config_versions", step.config_bundle_id) or {}
    pinned_native = step.leg_id in bundle.get('body', {}).get('structured_output_legs', [])
    if structured_output is None: structured_output = pinned_native
    if pinned_native and not structured_output: return GatewayResult(error_code='CONFIG_GENERATION_MISMATCH')
    # Schema and instructions are in the exact text counted and sent. JSON mode
    # constrains syntax; Pydantic (plus the caller's grounding checks) validates it.
    contents = json.dumps({"instruction": prompt, **({} if structured_output else {"output_schema": schema.model_json_schema()})}, separators=(",", ":"))
    if len(contents) > 100000 or not 1 <= input_limit <= 8192 or not 1 <= output_limit <= 3072:
        return GatewayResult(error_code="REQUEST_LIMIT")
    from .agent_profiles import ModelProfile, frozen_profile
    try:
        requested = ModelProfile(model_id=target_model, thinking_level=thinking_level or "MINIMAL",
            temperature=temperature, max_output_tokens=output_limit)
        if step.leg_id in {"A2", "A3", "A4", "A5"}:
            profile = frozen_profile(bundle, step.leg_id, repo.settings.active_model_id)
            if profile.model_id != target_model: return GatewayResult(error_code="CONFIG_MODEL_MISMATCH")
            if (profile.thinking_level != requested.thinking_level or profile.temperature != requested.temperature
                or profile.max_output_tokens is not None and profile.max_output_tokens != output_limit):
                return GatewayResult(error_code="CONFIG_GENERATION_MISMATCH")
    except ValueError:
        return GatewayResult(error_code="CONFIG_GENERATION_MISMATCH")
    reserved = False
    clock = monotonic()
    usage = {}
    try:
        if step.worker_lease: repo.check_worker_lease(step.worker_lease, reserve_seconds=65)
        from .pricing import frozen_card, estimate
        rate_card_id, card = frozen_card(repo, step)
        step = step.model_copy(update={"rate_card_id": rate_card_id, "model_id": target_model})
        config = {
            "response_mime_type": "application/json", "max_output_tokens": output_limit,
            "thinking_config": {"thinking_level": requested.thinking_level},
            "automatic_function_calling": {"disable": True},
            **({"service_tier": "standard"} if card is not None else {})}
        if structured_output:
            native_legs = bundle.get('body', {}).get('structured_output_legs', [])
            allowed = (step.leg_id == 'EVALUATE' and bundle.get('body', {}).get('structured_output')) or (
                step.leg_id in native_legs and set(native_legs) <= {'A2', 'A3', 'A4', 'A5'})
            if not allowed:
                return GatewayResult(error_code='CONFIG_GENERATION_MISMATCH')
            config['response_json_schema'] = bundle.get('body', {}).get('native_response_schemas', {}).get(step.leg_id, schema.model_json_schema())
        if requested.temperature is not None: config["temperature"] = requested.temperature
        snapshot = {"contents": contents}
        if structured_output or "generation_profiles" in bundle.get("body", {}):
            snapshot.update(generation_config=config, model_id=target_model)
        with _provider_client(repo.settings) as client:
            _pace_local(repo.settings)
            count = client.models.count_tokens(model=target_model, contents=None if structured_output and repo.settings.mode == 'local' else contents,
                **({"config": _count_config(repo.settings, target_model, contents, config['response_json_schema'])} if structured_output else {})).total_tokens
            if type(count) is not int or count < 0: return GatewayResult(error_code="PRECOUNT_UNAVAILABLE")
            if count > input_limit: return GatewayResult(error_code="CONTEXT_LIMIT", context_precount_tokens=count)
            step = step.model_copy(update={"context_precount_tokens": count, "input_snapshot": snapshot,
                "input_hash": content_hash(snapshot) if "generation_config" in snapshot else content_hash(contents), "model_id": target_model})
            _pace_local(repo.settings)
            if not repo.reserve_model_attempt(step): return GatewayResult(error_code="ATTEMPT_ALREADY_RESERVED", step_id=step.id)
            reserved = True
            response = client.models.generate_content(model=target_model, contents=contents, config=config)
            metadata = response.usage_metadata
            fields = {"prompt_tokens": "prompt_token_count", "cached_prompt_tokens": "cached_content_token_count",
                "candidate_tokens": "candidates_token_count", "thought_tokens": "thoughts_token_count",
                "tool_prompt_tokens": "tool_use_prompt_token_count", "total_tokens": "total_token_count"}
            usage = {key: value if type(value := getattr(metadata, name, None)) is int and value >= 0 else None for key, name in fields.items()}
            tier = getattr(metadata, "service_tier", None)
            tier = getattr(tier, "value", tier)
            usage.update({"usage_status": "PROVIDER_REPORTED" if any(value is not None for value in usage.values()) else "UNKNOWN",
                "provider_usage_raw": {key: value for key, value in usage.items() if value is not None},
                "provider_service_tier": tier if type(tier) is str else None,
                "provider_request_id": str(response.response_id)[:128] if response.response_id else None})
            usage.update(estimate(card, usage, repo.settings, step))
            parsed = schema.model_validate_json(response.text or "")
            result = parsed.model_dump(mode="json")
            validation = {"schema_pass": True}
            if evidence is not None:
                from .validation import validate_model_output
                try: findings = validate_model_output(parsed, evidence)
                except ValueError as exc: raise ValidationBoundaryError() from exc
                validation.update({key: value for key, value in findings.items() if key != "output"})
                validation["evidence_pass"] = True
            accepted = repo.seal_provider_attempt(step, {**usage, "state": "SUCCEEDED", "output": result,
                "validation": validation, "execution_provenance": "REAL_PROVIDER", "finished_at": now(), "duration_ms": int((monotonic() - clock) * 1000),
                "error_code": None})
            if not accepted: return GatewayResult(outcome="UNKNOWN_OUTCOME", error_code="ATTEMPT_ALREADY_SEALED", step_id=step.id)
            return GatewayResult(outcome="SUCCEEDED", error_code=None, output=result, step_id=step.id)
    except Conflict as exc:
        return GatewayResult(outcome="UNKNOWN_OUTCOME" if reserved else "NOT_SENT", error_code=str(exc), step_id=step.id if reserved else None)
    except Exception as exc:
        # Exception strings can contain prompts, headers or keys: never store them.
        code = getattr(exc, "code", None)
        invalid = isinstance(exc, (ValidationError, ValidationBoundaryError)) and bool(usage)
        rejected = type(code) is int and 400 <= code < 500 and code != 408
        error = "OUTPUT_INVALID" if invalid else f"PROVIDER_HTTP_{code}" if type(code) is int and 400 <= code <= 599 else "PROVIDER_OUTCOME_UNKNOWN"
        if reserved:
            schema_fields = set(schema.model_fields) | {name for definition in schema.model_json_schema().get('$defs', {}).values()
                for name in definition.get('properties', {})}
            accepted = repo.seal_provider_attempt(step, {**usage,
                "state": "FAILED" if invalid or rejected else "UNKNOWN_OUTCOME", "output": None,
                "execution_provenance": "REAL_PROVIDER" if invalid or rejected else "UNKNOWN",
                **({"validation": {"schema_pass": False, "error_types": sorted({item['type'] for item in exc.errors()}),
                    "missing_fields": sorted({item['loc'][-1] for item in exc.errors() if item['type'] == 'missing' and item['loc']
                        and item['loc'][-1] in schema_fields})}}
                    if isinstance(exc, ValidationError) else {}),
                **({"validation": {"provider_rejection": "SCHEMA_CONFIGURATION" if "schema" in str(exc).lower() else "INVALID_REQUEST"}}
                    if code == 400 else {}),
                "retry_delay_seconds": _retry_delay(exc) if rejected else None,
                "error_code": error, "finished_at": now(), "duration_ms": int((monotonic() - clock) * 1000)})
            if not accepted: return GatewayResult(outcome="UNKNOWN_OUTCOME", error_code="ATTEMPT_ALREADY_SEALED", step_id=step.id)
        return GatewayResult(outcome="FAILED" if invalid or rejected else "UNKNOWN_OUTCOME" if reserved else "NOT_SENT", error_code=error, step_id=step.id if reserved else None)


class ValidationBoundaryError(ValueError):
    """A grounded-output check failed; no provider text is exposed."""
