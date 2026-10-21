"""ResearchSpec: the strict, machine-readable hypothesis definition Claude produces (instead of backtest code).

Everything executable is declarative and drawn from a closed vocabulary of CAUSAL features, so a spec cannot
reference future data. Validation failures raise ResearchSpecError and nothing is registered or run."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .costs import CostModel
from .schema import TIMEFRAME_MINUTES

FEATURE_KINDS = {"close", "sma", "ema", "atr", "ret", "zscore", "rolling_high", "rolling_low",
                 "efficiency_ratio", "spread_pips"}
KINDS_NEED_N = FEATURE_KINDS - {"close", "spread_pips"}
OPS = {">", ">=", "<", "<=", "crosses_above", "crosses_below"}
REGIME_DIMENSIONS = {"session", "volatility", "trend_range", "year", "direction"}
MAX_PARAM_VARIANTS_BLOCK = 30


class ResearchSpecError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mechanism(_Strict):
    who_provides_opportunity: str = Field(min_length=10)
    why_it_may_persist: str = Field(min_length=10)
    edge_should_exist_when: str = Field(min_length=5)
    edge_should_disappear_when: str = Field(min_length=5)


class DataRequirements(_Strict):
    provider: str
    timeframe: str = "M15"
    source_timeframe: str = "M1"
    min_bars_per_period: int = Field(200, ge=1)

    @model_validator(mode="after")
    def _tf(self):
        for tf in (self.timeframe, self.source_timeframe):
            if tf not in TIMEFRAME_MINUTES:
                raise ValueError(f"unknown timeframe {tf}")
        if TIMEFRAME_MINUTES[self.timeframe] < TIMEFRAME_MINUTES[self.source_timeframe]:
            raise ValueError("timeframe finer than source_timeframe")
        return self


class FeatureDef(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    kind: str
    n: int | None = Field(None, ge=1, le=5000)

    @model_validator(mode="after")
    def _k(self):
        if self.kind not in FEATURE_KINDS:
            raise ValueError(f"feature kind '{self.kind}' not allowed (causal vocabulary: {sorted(FEATURE_KINDS)})")
        if self.kind in KINDS_NEED_N and self.n is None:
            raise ValueError(f"feature kind '{self.kind}' requires n")
        return self


class Condition(_Strict):
    left: str
    op: str
    right: float | str

    @field_validator("op")
    @classmethod
    def _op(cls, v):
        if v not in OPS:
            raise ValueError(f"op must be one of {sorted(OPS)}")
        return v


class SignalDef(_Strict):
    long_when: list[Condition] = Field(default_factory=list)
    short_when: list[Condition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _some(self):
        if not self.long_when and not self.short_when:
            raise ValueError("signal needs long_when and/or short_when conditions")
        return self


class ExitDef(_Strict):
    atr_feature: str
    stop_atr_mult: float = Field(gt=0, le=20)
    target_r: float | None = Field(None, gt=0, le=50)
    max_hold_bars: int = Field(gt=0, le=5000)


class Period(_Strict):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, v):
        if v.tzinfo is None:
            raise ValueError("period boundaries must be timezone-aware UTC")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _order(self):
        if self.end <= self.start:
            raise ValueError("period end must be after start")
        return self


class Sensitivity(_Strict):
    spread_multipliers: list[float] = Field(default_factory=lambda: [2.0])
    slippage_per_side: list[float] = Field(default_factory=list)
    # parameter neighbours: list of {feature_name: new_n}. Counted toward multiple-testing.
    parameter_variants: list[dict[str, int]] = Field(default_factory=list)


class RejectionCriteria(_Strict):
    min_trades_per_period: int = Field(30, ge=1)
    min_net_expectancy_r: float = 0.0
    max_validation_degradation: float = Field(0.5, ge=0, le=1, description="allowed fractional drop dev->validation")
    max_drawdown_r: float = Field(20.0, gt=0)
    require_positive_after_2x_spread: bool = True


class ResearchSpec(_Strict):
    title: str = Field(min_length=5)
    hypothesis: str = Field(min_length=20)
    mechanism: Mechanism
    instruments: list[str] = Field(min_length=1)
    data: DataRequirements
    features: list[FeatureDef] = Field(min_length=1)
    signal: SignalDef
    exit: ExitDef
    costs: CostModel = Field(default_factory=CostModel)
    development: Period
    validation: Period
    untouched_test: Period = Field(validation_alias=AliasChoices("untouched_test", "final_untouched"),
                                   description="FINAL_UNTOUCHED period: never loaded until explicitly promoted to final testing")
    embargo_bars: int = Field(0, ge=0, description="bars skipped between periods to stop feature-window leakage")
    regime_tests: list[str] = Field(default_factory=lambda: ["session", "volatility", "trend_range", "year", "direction"])
    sensitivity: Sensitivity = Field(default_factory=Sensitivity)
    rejection: RejectionCriteria = Field(default_factory=RejectionCriteria)
    diagnostic: bool = Field(False, description="pipeline smoke test; never evidence of an edge")
    diagnostic_kind: Literal["SYNTHETIC_DIAGNOSTIC", "REAL_DATA_DIAGNOSTIC"] | None = None
    differs_from: list[str] = Field(default_factory=list, description="experiment IDs this materially differs from, with reason in hypothesis")
    differs_because: str = ""

    @field_validator("instruments")
    @classmethod
    def _inst(cls, v):
        return sorted({i.upper() for i in v})

    @model_validator(mode="after")
    def _cross(self):
        names = [f.name for f in self.features]
        if len(set(names)) != len(names):
            raise ValueError("duplicate feature names")
        known = set(names) | {"close"}
        for cond in (*self.signal.long_when, *self.signal.short_when):
            if cond.left not in known:
                raise ValueError(f"condition references undefined feature '{cond.left}'")
            if isinstance(cond.right, str) and cond.right not in known:
                raise ValueError(f"condition references undefined feature '{cond.right}'")
        kinds = {f.name: f.kind for f in self.features}
        if kinds.get(self.exit.atr_feature) != "atr":
            raise ValueError("exit.atr_feature must name an 'atr' feature")
        d, v, t = self.development, self.validation, self.untouched_test
        if not (d.end <= v.start and v.end <= t.start):
            raise ValueError("periods must be strictly ordered and non-overlapping: development < validation < untouched_test")
        for dim in self.regime_tests:
            if dim not in REGIME_DIMENSIONS:
                raise ValueError(f"unknown regime dimension '{dim}'")
        if len(self.sensitivity.parameter_variants) > MAX_PARAM_VARIANTS_BLOCK:
            raise ValueError(f"{len(self.sensitivity.parameter_variants)} parameter variants exceeds hard limit "
                             f"{MAX_PARAM_VARIANTS_BLOCK}: unrestricted search is not allowed")
        for var in self.sensitivity.parameter_variants:
            for fname in var:
                if fname not in kinds or kinds[fname] not in KINDS_NEED_N:
                    raise ValueError(f"parameter variant references unknown/non-parametric feature '{fname}'")
        if self.diagnostic_kind and not self.diagnostic:
            raise ValueError("diagnostic_kind requires diagnostic=true")
        if self.differs_from and len(self.differs_because.strip()) < 20:
            raise ValueError("differs_from requires a substantive differs_because")
        return self

    # ---- identity -------------------------------------------------------------------------------
    def canonical_json(self) -> str:
        d = self.model_dump(mode="json")
        if d.get("diagnostic_kind") is None:
            d.pop("diagnostic_kind", None)  # keeps hashes of specs frozen before this field existed
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    def spec_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def max_lookback_bars(self) -> int:
        return max([f.n or 1 for f in self.features] + [self.exit.max_hold_bars // 10 or 1])

    def fingerprint(self) -> str:
        """Structure-only identity (ignores prose, periods, parameter values) used for equivalence search."""
        def kind_of(name: str) -> str:
            return next((f.kind for f in self.features if f.name == name), "close")

        structure = {
            "instruments": self.instruments, "tf": self.data.timeframe,
            "kinds": sorted(f.kind for f in self.features),
            "signal": {side: sorted((kind_of(c.left), c.op, kind_of(c.right) if isinstance(c.right, str) else "const")
                                    for c in getattr(self.signal, side)) for side in ("long_when", "short_when")},
            "target": self.exit.target_r is not None,
        }
        return hashlib.sha256(json.dumps(structure, sort_keys=True, default=str).encode()).hexdigest()[:16]


def parse_spec(payload: dict | str) -> ResearchSpec:
    """Validate a ResearchSpec from a dict/JSON string. Raises ResearchSpecError (never a partial spec)."""
    try:
        if isinstance(payload, str):
            return ResearchSpec.model_validate_json(payload)
        return ResearchSpec.model_validate(payload)
    except ValidationError as exc:
        raise ResearchSpecError("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc
