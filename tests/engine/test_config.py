from __future__ import annotations

import pytest

from recsys.engine import EngineConfig, Tier, TierWeights


def test_roundtrip_and_hash_are_stable() -> None:
    cfg = EngineConfig(eps=0.1, warm=TierWeights(0.7, 0.2, 0.1))
    again = EngineConfig.from_dict(cfg.to_dict())
    assert again == cfg
    assert again.config_hash() == cfg.config_hash()
    assert EngineConfig().config_hash() != cfg.config_hash()


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown config keys"):
        EngineConfig.from_dict({"nope": 1})


@pytest.mark.parametrize(
    "kwargs",
    [{"pool_size": 0}, {"eps": 1.5}, {"mmr_lambda": -0.1}, {"recent_decay": 0.0}],
)
def test_invalid_values_fail_at_construction(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        EngineConfig(**kwargs)  # type: ignore[arg-type]


def test_tier_weights_must_have_mass() -> None:
    with pytest.raises(ValueError):
        TierWeights(0.0, 0.0, 0.0)


def test_tiers() -> None:
    cfg = EngineConfig()
    assert cfg.tier_of(0) is Tier.COLD
    assert cfg.tier_of(1) is Tier.LUKEWARM
    assert cfg.tier_of(4) is Tier.LUKEWARM
    assert cfg.tier_of(5) is Tier.WARM
