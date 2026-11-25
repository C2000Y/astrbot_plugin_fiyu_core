"""Configuration and accounting units for optional media calls."""

from __future__ import annotations

import math
import re
from datetime import date

from .util import unsafe_secret

MEDIA_UNITS = {"image": "images", "stt": "seconds", "tts": "characters", "omni": "tokens"}
MEDIA_LABELS = {"image": "绘图与编辑", "stt": "语音识别", "tts": "语音合成", "omni": "音视频理解"}
DASHSCOPE_MODEL_TYPES = {
    "image": ("wan2.7-image", "wan2.7-image-pro"),
    "stt": ("fun-asr-flash-2026-06-15",),
    "omni": ("qwen3.5-omni-plus", "qwen3.8-omni-flash"),
}
DEFAULT_MODELS = {"image": "wan2.7-image", "stt": "", "tts": "", "omni": "qwen3.8-omni-flash"}
WORKSPACE_ORIGIN = re.compile(r"https://[a-z0-9-]{1,60}\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com")


def valid_origin(value):
    return value in ("https://dashscope.aliyuncs.com", "https://dashscope-intl.aliyuncs.com") or bool(
        WORKSPACE_ORIGIN.fullmatch(value)
    )


def media_defaults(kind):
    maximum = {"image": 1, "stt": 30, "tts": 150, "omni": 18000}[kind]
    daily = {"image": 2, "stt": 120, "tts": 600, "omni": 36000}[kind]
    return {
        "enabled": False, "provider": DEFAULT_MODELS[kind], "allowed_groups": [], "allow_private": False,
        "max_units": maximum, "daily_units": daily, "scope_daily_units": daily,
        "timeout_seconds": 120 if kind == "image" else 35, "price_per_unit": 0.0,
        "free_only": False, "free_units_per_day": 0, "free_until": "",
        **({"size": "1024*1024"} if kind == "image" else {}),
        **({"text_with_audio": False, "fallback_text": True} if kind == "tts" else {}),
    }


def validate_media(kind, settings):
    default = media_defaults(kind)
    if settings.keys() - default.keys():
        raise ValueError("未知媒体配置项")
    merged = {**default, **settings}
    for key, value in merged.items():
        original = default[key]
        if key == "price_per_unit":
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 10000:
                raise ValueError("价格必须为有限的非负数")
        elif type(value) is not type(original):
            raise ValueError(f"{key} 类型无效")
    for key in ("max_units", "daily_units", "scope_daily_units", "free_units_per_day"):
        high = {"image": 100, "stt": 86400, "tts": 500000, "omni": 5000000}[kind]
        if not 0 <= merged[key] <= high:
            raise ValueError(f"{key} 超出媒体限额范围")
    if merged["max_units"] > {"image": 4, "stt": 120, "tts": 500, "omni": 100000}[kind]:
        raise ValueError("单次媒体限额过高")
    if not 5 <= merged["timeout_seconds"] <= 180:
        raise ValueError("媒体超时须为 5～180 秒")
    provider = merged["provider"].strip()
    if len(provider) > 160 or unsafe_secret(provider) or (kind in ("image", "omni") and provider not in DASHSCOPE_MODEL_TYPES[kind]):
        raise ValueError("模型未注册或 Provider ID 过长")
    merged["provider"] = provider
    if merged["free_until"]:
        date.fromisoformat(merged["free_until"])
    if kind == "image" and merged["size"] not in ("1024*1024", "1280*720", "720*1280", "2048*2048"):
        raise ValueError("绘图尺寸未验证")
    return merged
