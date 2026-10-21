"""Typed eligibility rules for private flood insurance programs.

Every rule carries Evidence: a short quote from the source document and its page.
The verifier (verify.py) rejects any rule whose quote isn't actually in the document,
so a rule only reaches the coverage engine if it can be traced back to the source.

Labels in evals/labels use the same models with evidence left out.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
]
State = Enum("State", {s: s for s in US_STATES}, type=str)  # type: ignore[misc]  # built from the list above


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class Evidence(Strict):
    quote: str = Field(description="Exact words copied from the document that state this rule. Under 25 words. Do not paraphrase.")
    page: int = Field(description="1-based page number where the quote appears.")


class AvailabilityMode(str, Enum):
    ALL_STATES = "all_states"
    ALL_EXCEPT = "all_except"
    ONLY_LISTED = "only_listed"
    NOT_STATED = "not_stated"


class Availability(Strict):
    mode: AvailabilityMode
    states: list[State] = Field(default_factory=list, description="Two-letter codes: the excluded states for all_except, the included states for only_listed, empty otherwise.")
    evidence: Evidence | None = None


class LocationKind(str, Enum):
    """Location-based exclusions, which can be checked on a map."""
    CBRS = "cbrs"                      # Coastal Barrier Resources System / CBRA zones
    FLOODWAY = "floodway"              # FEMA regulatory floodway
    OVER_WATER = "over_water"          # building partly or entirely over water
    ISLAND = "island"
    BARRIER_ISLAND = "barrier_island"


class LocationExclusion(Strict):
    kind: LocationKind
    evidence: Evidence | None = None


class PropertyKind(str, Enum):
    """Exclusions about the building or its history, which a map can't check."""
    MANUFACTURED_HOME = "manufactured_home"
    CONDO = "condo"
    PRIOR_LOSSES = "prior_losses"
    SEVERE_REPETITIVE_LOSS = "severe_repetitive_loss"
    SUBGRADE_CONTENTS = "subgrade_contents"
    NFIP_GRANDFATHERED = "nfip_grandfathered"
    OTHER = "other"


class PropertyExclusion(Strict):
    kind: PropertyKind
    detail: str | None = Field(default=None, description="Thresholds or specifics, e.g. 'more than 1 loss or a single loss over $25K'.")
    evidence: Evidence | None = None


class FloodZone(str, Enum):
    A = "A"   # includes AE, AH, AO, AR, A99
    V = "V"   # includes VE
    B = "B"
    C = "C"
    X = "X"
    D = "D"


class ZoneRule(str, Enum):
    ELIGIBLE = "eligible"
    ELIGIBLE_IF_ELEVATED_ABOVE_BFE = "eligible_if_elevated_above_bfe"
    ELIGIBLE_IF_NOT_SURGE_EXPOSED = "eligible_if_not_surge_exposed"
    INELIGIBLE = "ineligible"


class ZoneCondition(Strict):
    zones: list[FloodZone]
    rule: ZoneRule
    evidence: Evidence | None = None


class LimitKind(str, Enum):
    DWELLING = "dwelling"
    CONTENTS = "contents"
    OTHER_STRUCTURES = "other_structures"
    LOSS_OF_USE = "loss_of_use"
    TOTAL_INSURED_VALUE = "total_insured_value"
    INCREASED_COST_OF_COMPLIANCE = "increased_cost_of_compliance"


class Limit(Strict):
    kind: LimitKind
    amount_usd: int = Field(description="Maximum in whole US dollars. Only fixed dollar amounts; skip percentages.")
    evidence: Evidence | None = None


class Product(Strict):
    insurer: str = Field(description="Company or brand selling the program, e.g. 'Neptune Flood'.")
    program: str = Field(description="Program or product name as written, e.g. 'FloodPlus'. Repeat the insurer if there is no separate name.")
    availability: Availability
    surplus_lines: bool | None = Field(default=None, description="True if sold on a surplus lines basis, false if admitted, null if not stated.")
    elevation_certificate_required: bool | None = None
    waiting_period_days: int | None = Field(default=None, description="Standard waiting period in days, null if not stated.")
    location_exclusions: list[LocationExclusion] = Field(default_factory=list)
    property_exclusions: list[PropertyExclusion] = Field(default_factory=list)
    zone_conditions: list[ZoneCondition] = Field(default_factory=list)
    limits: list[Limit] = Field(default_factory=list)


class Extraction(Strict):
    """What the model returns for one document."""
    products: list[Product] = Field(description="One entry per private flood program the document describes. Skip the NFIP.")
