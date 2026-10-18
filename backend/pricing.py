"""Conservative text-token estimates from a frozen, explicitly verified rate card."""
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator

from .dataset import content_hash
from .usage import reconcile

Rate = Annotated[StrictInt, Field(ge=0, le=10**12)]


class RateCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verified: StrictBool
    verified_by: str = Field(min_length=1, max_length=128)
    provider: Literal["GOOGLE_CLOUD"]
    service: Literal["aiplatform.googleapis.com"]
    currency: Literal["USD"]
    model_id: str = Field(min_length=1, max_length=128)
    location: str = Field(min_length=1, max_length=64)
    billing_mode: Literal["STANDARD_ON_DEMAND_TEXT"]
    usage_semantics: Literal["PROMPT_INCLUDES_CACHE_THOUGHTS_SEPARATE_V1"]
    source_url: str = Field(max_length=2048)
    input_sku: str = Field(min_length=1, max_length=200)
    cached_input_sku: str = Field(min_length=1, max_length=200)
    output_sku: str = Field(min_length=1, max_length=200)
    effective_from: AwareDatetime
    effective_until: AwareDatetime
    max_input_tokens: Annotated[StrictInt, Field(ge=1, le=10**7)]
    uncached_prompt_usd_micros_per_million: Rate
    cached_prompt_usd_micros_per_million: Rate
    output_including_thoughts_usd_micros_per_million: Rate

    @model_validator(mode="after")
    def reviewed_scope(self):
        source = urlsplit(self.source_url)
        if (self.verified is not True or source.scheme != "https" or source.hostname not in {"cloud.google.com", "docs.cloud.google.com", "ai.google.dev"}
            or source.username or source.password or source.query or self.effective_from >= self.effective_until):
            raise ValueError("Rate card needs an official source and a valid effective interval")
        return self


def parse_card(raw):
    if not isinstance(raw, str) or len(raw.encode()) > 16000:
        raise ValueError("Verified rate card configuration is invalid")
    try: return RateCard.model_validate_json(raw, strict=True)
    except (ValueError, ValidationError):
        raise ValueError("Verified rate card configuration is invalid") from None


def configured_card(settings):
    if not settings.verified_rate_card_json: return None
    card = parse_card(settings.verified_rate_card_json)
    if (settings.mode != "cloud" or card.model_id != settings.model_id or card.location != settings.verified_cloud_location):
        raise ValueError("Verified rate card does not match the cloud model/location")
    return card


def frozen_card(repo, step):
    """Read the run's pinned card, never silently select a newer deployment card."""
    if repo.settings.mode != "cloud": return None, None
    bundle = repo.get("config_versions", step.config_bundle_id) or {}
    ident = bundle.get("component_ids", {}).get("RATE_CARD")
    if not ident: return None, None
    row = repo.get("config_versions", ident) or {}
    version = {key: row.get(key) for key in ("kind", "origin", "body")}
    if row.get("kind") != "RATE_CARD" or content_hash(version) != ident or row.get("content_hash") != ident:
        return None, None
    import json
    try: card = parse_card(json.dumps(row["body"]))
    except ValueError: return None, None
    return ident, card


def estimate(card, usage, settings, step, instant=None):
    unknown = {"cost_status": "UNKNOWN", "estimated_usd_micros": None}
    instant = instant or datetime.now(timezone.utc)
    categories = reconcile(usage)
    if (card is None or settings.mode != "cloud" or card.model_id != step.model_id
        or card.location != settings.verified_cloud_location or not card.effective_from <= instant < card.effective_until
        or usage.get("provider_service_tier") != "standard" or not categories
        or usage["prompt_tokens"] > card.max_input_tokens or categories["tool_prompt_tokens"] != 0):
        return unknown
    numerator = (categories["uncached_prompt_tokens"] * card.uncached_prompt_usd_micros_per_million
        + categories["cached_prompt_tokens"] * card.cached_prompt_usd_micros_per_million
        + (categories["candidate_tokens"] + categories["thought_tokens"]) * card.output_including_thoughts_usd_micros_per_million)
    # Round the sum once, upward to a whole micro-USD. This is not an invoice.
    return {"cost_status": "ESTIMATED", "estimated_usd_micros": (numerator + 999999) // 1000000}
