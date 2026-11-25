"""Validated per-session behavior overrides over global configuration."""

from __future__ import annotations

PRESETS = {
    "quiet": {"decision": {"ordinary_check_probability": 0.45, "dynamic_enabled": True,
                           "max_unsolicited_replies": 2, "unsolicited_gap_seconds": 35}},
    "normal": {"decision": {"ordinary_check_probability": 0.35, "dynamic_enabled": True,
                            "max_unsolicited_replies": 2, "unsolicited_gap_seconds": 45}},
    "busy": {"decision": {"ordinary_check_probability": 0.15, "dynamic_enabled": True,
                          "max_unsolicited_replies": 1, "unsolicited_gap_seconds": 75}},
    "questions": {"decision": {"ordinary_check_probability": 0.0, "dynamic_enabled": False}},
}

OVERRIDE_FIELDS = {
    "decision": {"ordinary_check_probability", "dynamic_enabled", "max_unsolicited_replies",
                 "unsolicited_gap_seconds"},
    "reply": {"vision_strategy"},
}
VISION_LEVELS = ("economy", "balanced", "full")


def effective_config(cfg, scope):
    override = cfg.get("sessions", {}).get("overrides", {}).get(scope)
    if not override:
        return cfg
    changed = {**cfg, **{section: {**cfg[section], **values} for section, values in override.items()}}
    if "reply" in override:
        level = min(VISION_LEVELS.index(cfg["reply"]["vision_strategy"]),
                    VISION_LEVELS.index(changed["reply"]["vision_strategy"]))
        changed["reply"]["vision_strategy"] = VISION_LEVELS[level]
    return changed
