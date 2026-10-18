"""Capability-driven agent model profiles loaded from Secret Manager or environment."""
from enum import StrEnum
import json
import os
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class AgentCapability(StrEnum):
    FAST_CLASSIFICATION = "FAST_CLASSIFICATION"
    STRUCTURED_REASONING = "STRUCTURED_REASONING"
    DEEP_INVESTIGATION = "DEEP_INVESTIGATION"
    META_EVOLUTION = "META_EVOLUTION"


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    capability: AgentCapability = AgentCapability.STRUCTURED_REASONING
    model_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,127}$")
    description: str = Field(default="", max_length=500)
    thinking_level: Literal["MINIMAL", "LOW", "MEDIUM", "HIGH"] = "MINIMAL"
    temperature: float | None = Field(default=0.2, strict=True, ge=0, le=2, allow_inf_nan=False)
    max_output_tokens: int | None = Field(default=None, strict=True, ge=1, le=3072)


DEFAULT_PROFILES = {
    "profiles": {
        "fast_triage": {
            "capability": AgentCapability.FAST_CLASSIFICATION.value,
            "model_id": "gemini-2.5-flash-lite",
            "description": "Sub-second structured triage and categorization",
            "thinking_level": "MINIMAL",
            "temperature": 0.1,
        },
        "risk_explainer": {
            "capability": AgentCapability.STRUCTURED_REASONING.value,
            "model_id": "gemini-2.5-flash",
            "description": "Explains code-computed risk scores, counter-evidence, and boundaries",
            "thinking_level": "MINIMAL",
            "temperature": 0.2,
        },
        "deep_investigation": {
            "capability": AgentCapability.DEEP_INVESTIGATION.value,
            "model_id": "gemini-2.5-pro",
            "description": "Multi-hop evidence synthesis, complex reasoning, and cited brief",
            "thinking_level": "MINIMAL",
            "temperature": 0.2,
        },
        "meta_evolution": {
            "capability": AgentCapability.META_EVOLUTION.value,
            "model_id": "gemini-2.5-pro",
            "description": "Analyzes historical noise distributions to propose rule changes",
            "thinking_level": "MINIMAL",
            "temperature": 0.1,
        },
    },
    "leg_mapping": {
        "A3": "fast_triage",
        "A2": "risk_explainer",
        "A4": "deep_investigation",
        "A5": "meta_evolution",
    },
}


class AgentProfilesRegistry:
    def __init__(self, raw_json: str | None = None, fallback_model: str = "gemini-3.5-flash"):
        self.fallback_model = fallback_model
        try:
            if raw_json is not None and (not isinstance(raw_json, str) or len(raw_json.encode()) > 16000):
                raise ValueError()
            self.is_custom = bool(raw_json and raw_json.strip())
            data = json.loads(raw_json) if self.is_custom else DEFAULT_PROFILES
            if (not isinstance(data, dict) or data.keys() - {"version", "profiles", "leg_mapping"}
                or not isinstance(data.get("profiles"), dict) or not 1 <= len(data["profiles"]) <= 16
                or not isinstance(data.get("leg_mapping"), dict)
                or set(data["leg_mapping"]) != {"A2", "A3", "A4", "A5"}
                or any(not isinstance(value, str) or value not in data["profiles"] for value in data["leg_mapping"].values())):
                raise ValueError()
            self.profiles = {key: ModelProfile.model_validate({**value,
                "model_id": value.get("model_id") if self.is_custom else fallback_model})
                for key, value in data["profiles"].items()}
            self.leg_mapping = dict(data["leg_mapping"])
            self.frozen_profiles  # Validate per-leg output ceilings before any execution.
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid agent profiles configuration") from None

    def get_for_leg(self, leg_id: str) -> ModelProfile:
        profile_key = self.leg_mapping.get(leg_id)
        if profile_key and profile_key in self.profiles:
            return self.profiles[profile_key]
        return ModelProfile(
            capability=AgentCapability.STRUCTURED_REASONING,
            model_id=self.fallback_model,
            description="Default fallback model profile",
            thinking_level="MINIMAL",
            temperature=0.2,
        )

    def get_for_capability(self, capability: AgentCapability | str) -> ModelProfile:
        cap_val = capability.value if isinstance(capability, AgentCapability) else capability
        for profile in self.profiles.values():
            if profile.capability == cap_val:
                return profile
        return self.get_for_leg("default")

    @property
    def authorized_models(self) -> frozenset[str]:
        """Configured choices only; funding/model authorization lives in Settings."""
        return frozenset(p.model_id for p in self.profiles.values()) | frozenset([self.fallback_model])

    @property
    def leg_model_map(self) -> dict[str, str]:
        return {leg: self.get_for_leg(leg).model_id for leg in ("A2", "A3", "A4", "A5")}

    @property
    def frozen_profiles(self) -> dict[str, dict]:
        from .prompts import LEGS
        result = {}
        for leg, spec in LEGS.items():
            profile = self.get_for_leg(leg)
            cap = profile.max_output_tokens if profile.max_output_tokens is not None else spec[2]
            if cap > spec[2]: raise ValueError("Profile exceeds the leg output ceiling")
            result[leg] = profile.model_copy(update={"max_output_tokens": cap}).model_dump(mode="json")
        return result


def frozen_profile(bundle, leg, fallback_model):
    body = (bundle or {}).get("body", {})
    if "generation_profiles" in body:
        profile = body["generation_profiles"].get(leg)
        if profile is None: raise ValueError("Missing frozen generation profile")
        profile = ModelProfile.model_validate(profile)
        from .prompts import LEGS
        if profile.max_output_tokens is None or profile.max_output_tokens > LEGS[leg][2]:
            raise ValueError("Invalid frozen output ceiling")
        return profile
    # Older single-model bundles retain the original MINIMAL/provider-temperature defaults.
    model_id = body.get("leg_models", {}).get(leg) or body.get("model_id") or fallback_model
    return ModelProfile(model_id=model_id, temperature=None)


def load_agent_profiles(raw_json: str | None = None, fallback_model: str = "gemini-3.5-flash") -> AgentProfilesRegistry:
    env_json = os.getenv("AGENT_PROFILES_JSON") if raw_json is None else raw_json
    return AgentProfilesRegistry(env_json, fallback_model=fallback_model)

