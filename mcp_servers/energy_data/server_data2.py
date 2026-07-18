"""
Archivo nuevo: implementacion perfecta del llm con un MCP
Developer notes: observability and typing contract for this MCP module.

Checklist at MCP tool boundaries:
- `load_consumption_data`: log INFO on success count, ERROR on malformed payload.
- `load_weather_data`: log INFO on success series lengths, ERROR on malformed payload.
- `get_generation_mix`: log endpoint attempts/usages and fallback day behavior.
- `get_daily_energy_context`: log INFO when context is complete, ERROR when any tool fails.

Generation endpoint/fallback contract:
- Endpoint order: `estructura-generacion`, `generacion`,
  `evolucion-renovable-no-renovable`, `estructura-renovables`.
- Day fallback window: `GEN_FALLBACK_DAYS = 3`.
- Fields to include in logs: `endpoint_attempted`, `endpoint_used`, `fallback_used`.

Generation analytics payload contract (`_build_generation_analytics` indicators):
- `renewable_share`, `gas_dependency`, `nuclear_share`, `thermal_share`,
  `carbon_intensity_estimated`, `defaults_applied`, `_has_exactly_24_values`.

Typing/docstring guidance:
- Use `dict[str, float]` for numeric maps.
- Return structured error dictionaries: `{"error": "message"}`.
- Keep helper docstrings short and include a return-shape example.

Expected response shapes (success/error):
- `load_consumption_data` -> `{"period": str, "consumption": list[int]}` | `{"error": str}`.
- `load_weather_data` -> weather dict with 24-value lists | `{"error": str}`.
- `get_generation_mix` -> analytics dict with `mix_mw`, `mix_pct`, `indicators` | `{"error": str}`.
- `get_daily_energy_context` -> demand + weather + generation_mix | `{"error": str, "details": {...}}`.
- Resource wrappers return the same shapes as their tool counterparts.

Environment file note:
- `_load_env_file` reads `.env` from repository root resolved as `Path(__file__).resolve().parents[2] / ".env"`.

MCP consumer note:
- This module is an MCP provider (`FastMCP` + `mcp.run(transport='stdio')`).
- Agent-side consumers should launch it via `MCPServerStdio(params)` and pass that server to Agent/Runner.
"""

import asyncio
import importlib
import json
import logging
import math
import os
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from mcp.server.fastmcp import FastMCP

try:
    from agents import OpenAIChatCompletionsModel
except Exception:
    OpenAIChatCompletionsModel = None

REE_BASE = "https://apidatos.ree.es/es/datos"
ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"
HTTP_TIMEOUT = 10.0
GENERATION_TIMEOUT = 15.0
RETRIES = 3
GENERATION_DAY_TIMEOUT = 25.0
GENERATION_TOTAL_TIMEOUT = 90.0
LLM_TIMEOUT = 20.0
LLM_RETRIES = 3
GEN_FALLBACK_DAYS = 3
LLM_MODEL_NAME = "gpt-4o-mini"
ENTSOE_DOCUMENT_TYPE = "A75"
ENTSOE_PROCESS_TYPE = "A16"
ENTSOE_DOMAIN_ES = "10YES-REE------0"
GEN_ENDPOINTS = [
    "estructura-generacion",
    "generacion",
    "evolucion-renovable-no-renovable",
    "estructura-renovables",
]

ENTSOE_PSR_TYPE_ALIASES = {
    "B01": "Biomass",
    "B02": "Fossil Brown coal/Lignite",
    "B03": "Fossil Coal-derived gas",
    "B04": "Fossil Gas",
    "B05": "Fossil Hard coal",
    "B06": "Fossil Oil",
    "B07": "Fossil Oil shale",
    "B08": "Fossil Peat",
    "B09": "Geothermal",
    "B10": "Hydro Pumped Storage",
    "B11": "Hydro Run-of-river and poundage",
    "B12": "Hydro Water Reservoir",
    "B13": "Marine",
    "B14": "Nuclear",
    "B15": "Other renewable",
    "B16": "Solar",
    "B17": "Waste",
    "B18": "Wind Offshore",
    "B19": "Wind Onshore",
    "B20": "Other",
}

RENEWABLE_TECH = {
    "wind",
    "photovoltaic solar",
    "thermal solar",
    "hydroelectric",
    "biomass",
    "renewable waste",
    "other renewables",
}

NON_RENEWABLE_TECH = {
    "nuclear",
    "combined cycle",
    "coal",
    "cogeneration",
    "non-renewable waste",
    "continuously concentrated hydroelectric plant",
    "diesel engines",
    "gas turbine",
    "steam turbine",
}

EMISSION_FACTORS = {
    "combined cycle": 0.35,
    "coal": 0.9,
    "nuclear": 0.0,
    "wind": 0.0,
}

missing_emission_factors: set[str] = set()

TECH_NAME_ALIASES = {
    "eólica": "Wind",
    "wind": "Wind",
    "solar fotovoltaica": "Photovoltaic Solar",
    "photovoltaic solar": "Photovoltaic Solar",
    "solar térmica": "Thermal Solar",
    "thermal solar": "Thermal Solar",
    "hidráulica": "Hydroelectric",
    "hydroelectric": "Hydroelectric",
    "biomasa": "Biomass",
    "biomass": "Biomass",
    "residuos renovables": "Renewable Waste",
    "renewable waste": "Renewable Waste",
    "nuclear": "Nuclear",
    "ciclo combinado": "Combined cycle",
    "combined cycle": "Combined cycle",
    "carbón": "Coal",
    "coal": "Coal",
    "cogeneración": "Cogeneration",
    "cogeneration": "Cogeneration",
    "residuos no renovables": "Non-renewable Waste",
    "non-renewable waste": "Non-renewable Waste",
    "hidroeléctrica de bombeo": "Continuously Concentrated Hydroelectric Plant",
    "continuously concentrated hydroelectric plant": "Continuously Concentrated Hydroelectric Plant",
    "motores diésel": "Diesel engines",
    "diesel engines": "Diesel engines",
    "turbina de gas": "Gas turbine",
    "gas turbine": "Gas turbine",
    "turbina de vapor": "Steam turbine",
    "steam turbine": "Steam turbine",
    "otras renovables": "Other Renewables",
    "other renewables": "Other Renewables",
    "other renewable": "Other Renewables",
    "solar": "Photovoltaic Solar",
    "wind onshore": "Wind",
    "wind offshore": "Wind",
    "hydro pumped storage": "Hydroelectric",
    "hydro run-of-river and poundage": "Hydroelectric",
    "hydro water reservoir": "Hydroelectric",
    "fossil gas": "Combined cycle",
    "fossil coal-derived gas": "Combined cycle",
    "fossil hard coal": "Coal",
    "fossil brown coal/lignite": "Coal",
    "waste": "Non-renewable Waste",
    "energy storage": "Hydroelectric",
    "generación total": "Total Generation",
    "total generation": "Total Generation",
}

TOTAL_TECH_KEYS = {
    "total generation",
}

logger = logging.getLogger(__name__)

OBSERVABILITY_CHECKLIST = [
    "load_consumption_data: INFO success count; ERROR malformed payload",
    "load_weather_data: INFO success lengths; ERROR malformed payload",
    "get_generation_mix: INFO endpoint attempt/use; WARNING fallback endpoint/day",
    "get_daily_energy_context: INFO complete context; ERROR failed composition",
]

GENERATION_ENDPOINT_LOG_CONTRACT: dict[str, Any] = {
    "endpoints": GEN_ENDPOINTS,
    "fallback_days": GEN_FALLBACK_DAYS,
    "required_fields": ["endpoint_attempted", "endpoint_used", "fallback_used"],
}

GENERATION_ANALYTICS_PAYLOAD_CONTRACT: dict[str, list[str]] = {
    "indicators": [
        "renewable_share",
        "gas_dependency",
        "nuclear_share",
        "thermal_share",
        "carbon_intensity_estimated",
        "defaults_applied",
        "_has_exactly_24_values",
    ]
}

MCP_RESPONSE_NOTES = {
    "load_consumption_data": {
        "success": {"period": "YYYY-MM-DD", "consumption": [123]},
        "error": {"error": "message"},
    },
    "load_weather_data": {
        "success": {
            "location": "City,CC",
            "period": "YYYY-MM-DD",
            "temperature": [0.0],
            "wind_speed": [0.0],
            "solar_irradiance": [0.0],
        },
        "error": {"error": "message"},
    },
    "get_generation_mix": {
        "success": {
            "date": "YYYY-MM-DD",
            "mix_mw": {"wind": 1.0},
            "mix_pct": {"wind": 100.0},
            "indicators": {"renewable_share": 1.0},
        },
        "error": {"error": "message"},
    },
    "get_daily_energy_context": {
        "success": {
            "period": "YYYY-MM-DD",
            "location": "City,CC",
            "demand": [100],
            "weather": {"temperature": [0.0]},
            "generation_mix": {"date": "YYYY-MM-DD"},
        },
        "error": {"error": "message", "details": {}},
    },
}


def _truncate_payload(raw_data: Any, limit: int = 500) -> str:
    text = str(raw_data)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _format_exception_message(error: Exception) -> str:
    message = str(error).strip()
    if message:
        return f"{error.__class__.__name__}: {message}"
    return error.__class__.__name__


def _client_loader_error(message: str) -> dict[str, str]:
    """Return standardized structured error shape: {'error': 'message'}."""
    return {"error": message}


def _error_response(
    code: str,
    message: str,
    component: str,
    provider: str,
    retryable: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "component": component,
            "provider": provider,
            "retryable": retryable,
        }
    }
    if extra:
        payload.update(extra)
    return payload


def _has_tool_error(result: dict[str, Any]) -> bool:
    return isinstance(result.get("error"), dict)

def _utc_today() -> date:
    return datetime.now(timezone.utc).date()

async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    retries: int = RETRIES,
) -> httpx.Response:
    for attempt in range(retries):
        try:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500 and attempt < retries - 1:
                await asyncio.sleep(2**attempt)
                continue
            raise
        except httpx.RequestError:
            if attempt < retries - 1:
                await asyncio.sleep(2**attempt)
                continue
            raise

def _ree_day_params(period_str: str, time_trunc: str = "day") -> dict:
    return {
        "start_date": f"{period_str}T00:00",
        "end_date": f"{period_str}T23:59",
        "time_trunc": time_trunc,
        "geo_limit": "peninsular",
    }

mcp = FastMCP("energy_server")
_ENV_LOADED = False
_CACHED_OPENAI_CLIENT: Any | None = None


def _load_env_file() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    _ENV_LOADED = True
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _get_model_name() -> str:
    return LLM_MODEL_NAME


def _get_openai_client() -> Any:
    global _CACHED_OPENAI_CLIENT
    if _CACHED_OPENAI_CLIENT is not None:
        return _CACHED_OPENAI_CLIENT

    _load_env_file()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not found; LLM client unavailable")
        return None

    try:
        openai_module = importlib.import_module("openai")
        async_openai = getattr(openai_module, "AsyncOpenAI", None)
    except Exception:
        async_openai = None

    if async_openai is None:
        logger.warning("openai.AsyncOpenAI not available; verify openai package installation")
        return None

    _CACHED_OPENAI_CLIENT = async_openai(api_key=api_key)
    return _CACHED_OPENAI_CLIENT


def get_wrapped_model(model_name: str) -> Any:
    """
    Return OpenAIChatCompletionsModel wrapper for Agent compatibility.
    Interoperable with traders.py Agent usage where model wrappers are passed to Agent(model=...).
    Example return shape: OpenAIChatCompletionsModel(model='gpt-4o-mini', openai_client=<AsyncOpenAI>). 
    """
    if OpenAIChatCompletionsModel is None:
        logger.warning("agents.OpenAIChatCompletionsModel not available; wrapper model unavailable")
        return _client_loader_error("OpenAIChatCompletionsModel not available")

    client = _get_openai_client()
    if client is None:
        return _client_loader_error("OPENAI_API_KEY missing or openai package not installed")

    chosen_model = model_name or _get_model_name()
    return OpenAIChatCompletionsModel(model=chosen_model, openai_client=client)


async def _query_llm(prompt: str, model_name: str | None = None) -> dict[str, Any]:
    client = _get_openai_client()
    if client is None:
        return _error_response(
            "llm_unavailable",
            "OPENAI_API_KEY missing or openai package not installed",
            "summarize_daily_energy_context",
            "openai",
            False,
        )

    chosen_model = model_name or _get_model_name()
    last_error: Exception | None = None
    started_at = time.perf_counter()
    prompt_preview = _truncate_payload(re.sub(r"\s+", " ", prompt).strip(), limit=160)
    logger.info(
        "LLM query started | model_name=%s | retries=%s | timeout=%s | prompt_length=%s | prompt_preview=%s",
        chosen_model,
        LLM_RETRIES,
        LLM_TIMEOUT,
        len(prompt),
        prompt_preview,
    )

    for attempt in range(LLM_RETRIES):
        attempt_started_at = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=chosen_model,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=LLM_TIMEOUT,
            )
            message = ""
            if response.choices:
                message = response.choices[0].message.content or ""
            attempt_latency_ms = round((time.perf_counter() - attempt_started_at) * 1000, 2)
            total_latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.info(
                "LLM query completed | model_name=%s | attempt=%s | has_content=%s | response_length=%s | latency_ms=%s | total_latency_ms=%s",
                chosen_model,
                attempt + 1,
                bool(message.strip()),
                len(message),
                attempt_latency_ms,
                total_latency_ms,
            )
            return {"model_name": chosen_model, "content": message}
        except TimeoutError as e:
            last_error = e
            attempt_latency_ms = round((time.perf_counter() - attempt_started_at) * 1000, 2)
            logger.warning(
                "LLM query timeout | model_name=%s | attempt=%s/%s | timeout=%s | latency_ms=%s | prompt_preview=%s",
                chosen_model,
                attempt + 1,
                LLM_RETRIES,
                LLM_TIMEOUT,
                attempt_latency_ms,
                prompt_preview,
            )
        except Exception as e:
            last_error = e
            attempt_latency_ms = round((time.perf_counter() - attempt_started_at) * 1000, 2)
            logger.warning(
                "LLM query attempt failed | model_name=%s | attempt=%s/%s | error=%s | latency_ms=%s | prompt_preview=%s",
                chosen_model,
                attempt + 1,
                LLM_RETRIES,
                e,
                attempt_latency_ms,
                prompt_preview,
            )

        if attempt < LLM_RETRIES - 1:
            await asyncio.sleep(2**attempt)

    total_latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.error(
        "LLM query failed after retries | model_name=%s | error=%s | total_latency_ms=%s | prompt_preview=%s",
        chosen_model,
        last_error,
        total_latency_ms,
        prompt_preview,
    )
    return _error_response(
        "llm_request_failed",
        f"llm request failed: {last_error}",
        "summarize_daily_energy_context",
        "openai",
        True,
    )

TOOL_RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    "load_consumption_data": {
        "type": "object",
        "required": ["period", "consumption"],
        "properties": {
            "period": {"type": "string", "format": "date"},
            "consumption": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Hourly electric demand in MW (24 or 25 points depending on calendar effects).",
            },
        },
        "error": {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message", "component", "provider", "retryable"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "component": {"type": "string"},
                        "provider": {"type": "string"},
                        "retryable": {"type": "boolean"},
                    },
                }
            },
        },
    },
    "load_weather_data": {
        "type": "object",
        "required": [
            "location",
            "period",
            "latitude",
            "longitude",
            "temperature",
            "wind_speed",
            "solar_irradiance",
        ],
        "properties": {
            "location": {"type": "string"},
            "period": {"type": "string", "format": "date"},
            "latitude": {"type": "number"},
            "longitude": {"type": "number"},
            "temperature": {"type": "array", "items": {"type": "number"}, "minItems": 24, "maxItems": 24},
            "wind_speed": {"type": "array", "items": {"type": "number"}, "minItems": 24, "maxItems": 24},
            "solar_irradiance": {"type": "array", "items": {"type": "number"}, "minItems": 24, "maxItems": 24},
        },
        "error": {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message", "component", "provider", "retryable"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "component": {"type": "string"},
                        "provider": {"type": "string"},
                        "retryable": {"type": "boolean"},
                    },
                }
            },
        },
    },
    "get_generation_mix": {
        "type": "object",
        "required": [
            "date",
            "total_mw",
            "mix_mw",
            "mix_pct",
            "generation_by_type",
            "classified",
            "indicators",
            "meta",
        ],
        "properties": {
            "date": {"type": "string", "format": "date"},
            "total_mw": {"type": "number"},
            "mix_mw": {"type": "object", "additionalProperties": {"type": "number"}},
            "mix_pct": {"type": "object", "additionalProperties": {"type": "number"}},
            "generation_by_type": {"type": "object"},
            "classified": {"type": "object"},
            "indicators": {"type": "object"},
            "meta": {"type": "object"},
        },
        "error": {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message", "component", "provider", "retryable"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "component": {"type": "string"},
                        "provider": {"type": "string"},
                        "retryable": {"type": "boolean"},
                    },
                }
            },
        },
    },
    "get_daily_energy_context": {
        "type": "object",
        "required": ["period", "location", "demand", "weather", "generation_mix", "partial_failure", "errors"],
        "properties": {
            "period": {"type": "string", "format": "date"},
            "location": {"type": "string"},
            "demand": {"type": ["array", "null"], "items": {"type": "integer"}},
            "weather": {
                "type": ["object", "null"],
                "required": ["latitude", "longitude", "temperature", "wind_speed", "solar_irradiance"],
            },
            "generation_mix": {"type": ["object", "null"]},
            "partial_failure": {"type": "boolean"},
            "errors": {"type": "object"},
        },
        "error": {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message", "component", "provider", "retryable"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "component": {"type": "string"},
                        "provider": {"type": "string"},
                        "retryable": {"type": "boolean"},
                    },
                }
            },
        },
    },
}


def _parse_period_date(period: str) -> date:
    """
    Convierte period (YYYY-MM-DD) a date.
    Lanza ValueError si el formato no es válido.
    """
    return datetime.strptime(period, "%Y-%m-%d").date()


def _is_actual_demand_curve(item: dict) -> bool:
    attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
    candidate_text = " ".join(
        [
            str(item.get("type", "")),
            str(attrs.get("title", "")),
            str(attrs.get("description", "")),
            str(attrs.get("nombre", "")),
        ]
    ).lower()
    return (
        "demanda real" in candidate_text
        or "actual demand" in candidate_text
        or candidate_text.strip() == "real"
    )


def _to_hourly_consumption(values: list[dict]) -> list[int]:
    hourly_buckets: dict[datetime, list[float]] = {}

    for item in values:
        if not isinstance(item, dict):
            continue

        value = item.get("value")
        dt_raw = item.get("datetime")
        if value is None or dt_raw is None:
            continue

        try:
            point_dt = datetime.fromisoformat(str(dt_raw).replace("Z", "+00:00"))
            numeric_value = float(value)
        except (ValueError, TypeError):
            continue

        hour_key = point_dt.replace(minute=0, second=0, microsecond=0)
        hourly_buckets.setdefault(hour_key, []).append(numeric_value)

    ordered_hours = sorted(hourly_buckets.items(), key=lambda element: element[0])
    consumption = [int(round(sum(series) / len(series))) for _, series in ordered_hours if series]

    logger.info("Demand points normalized to %s hourly values", len(consumption))
    if len(consumption) not in (24, 25):
        logger.error("Unexpected number of hourly demand values: %s", len(consumption))
        raise ValueError(
            f"Unexpected number of hourly values: {len(consumption)} (expected 24 or 25)"
        )

    return consumption


def _extract_generation_mix(data: dict[str, Any]) -> dict[str, list[Any]]:
    """
    Extrae series horarias de respuestas REE con distintos esquemas.
    Soporta claves de valores: values / valores.
    Returns:
        dict[str, list[Any]] con forma {'technology': [hourly values]}.
    """
    included = data.get("included", [])
    if not isinstance(included, list) or not included:
        return {}

    mix = {}
    for item in included:
        attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
        tech_name = (
            attrs.get("nombre")
            or item.get("type")
            or attrs.get("title")
            or "unknown"
        )
        values_block = attrs.get("values")
        if values_block is None:
            values_block = attrs.get("valores")

        if values_block is None:
            logger.debug(
                "Generation series skipped: missing values/valores | item=%s",
                _truncate_payload(item),
            )
            values_block = []

        if not isinstance(values_block, list):
            logger.debug(
                "Generation series skipped: values/valores is not a list | tech=%s | payload=%s",
                str(tech_name).strip(),
                _truncate_payload(values_block),
            )
            continue

        series = [v.get("value") if isinstance(v, dict) else v for v in values_block]
        mix[str(tech_name).strip()] = series

    return mix


def _canonical_tech_name(name: str) -> str:
    key = str(name).strip().lower()
    return str(TECH_NAME_ALIASES.get(key, key)).lower()


def _series_to_mw(series: list) -> float:
    numeric_values: list[float] = []
    for value in series:
        try:
            numeric_values.append(float(value))
        except (TypeError, ValueError):
            continue

    if not numeric_values:
        return 0.0

    return statistics.mean(numeric_values)


def _series_24_indicator(series: list[Any], use_nan: bool = False) -> int | float:
    if not isinstance(series, list):
        return math.nan if use_nan else 0
    if len(series) == 24:
        return 1
    if len(series) < 24:
        return math.nan if use_nan else 0
    return 0


def _normalize_series_to_24(series: list[Any]) -> list[float]:
    numeric_values: list[float] = []
    for value in series:
        try:
            numeric_values.append(float(value))
        except (TypeError, ValueError):
            continue

    if not numeric_values:
        return []

    length = len(numeric_values)
    target = 24
    if length == target:
        return numeric_values

    if target == 1:
        return [numeric_values[0]]

    if length == 1:
        return [numeric_values[0] for _ in range(target)]

    if length < target:
        normalized: list[float] = []
        for index in range(target):
            source_position = index * (length - 1) / (target - 1)
            left_index = int(math.floor(source_position))
            right_index = min(left_index + 1, length - 1)
            fraction = source_position - left_index
            left_value = numeric_values[left_index]
            right_value = numeric_values[right_index]
            interpolated = left_value + (right_value - left_value) * fraction
            normalized.append(interpolated)
        return normalized

    normalized = []
    for index in range(target):
        start = int(math.floor(index * length / target))
        end = int(math.floor((index + 1) * length / target))
        if end <= start:
            end = min(start + 1, length)
        bucket = numeric_values[start:end]
        if not bucket:
            bucket = [numeric_values[min(start, length - 1)]]
        normalized.append(sum(bucket) / len(bucket))
    return normalized


async def _fetch_generation_mix_with_fallback(
    client: httpx.AsyncClient,
    period_str: str,
    endpoints: list[str],
) -> tuple[dict[str, list[Any]] | None, str | None, Exception | dict[str, str] | None]:
    last_error: Exception | None = None
    last_endpoint: str | None = None

    for endpoint in endpoints:
        url = f"{REE_BASE}/generacion/{endpoint}"
        try:
            response = await fetch_with_retry(
                client,
                url,
                params=_ree_day_params(period_str, time_trunc="hour"),
                retries=3,
            )
            try:
                data = response.json()
            except json.JSONDecodeError:
                return None, endpoint, {
                    "code": "invalid_json",
                    "message": "Invalid JSON in generation provider response",
                    "endpoint": endpoint,
                    "period": period_str,
                }
            mix = _extract_generation_mix(data)
            if not mix:
                raise ValueError("unexpected API response structure")
            return mix, endpoint, None
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
            last_endpoint = endpoint

    return None, last_endpoint, last_error


def _entsoe_period_bounds(period_str: str) -> tuple[str, str]:
    day = _parse_period_date(period_str)
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M")


def _xml_text(element: ET.Element | None, path: str) -> str | None:
    if element is None:
        return None
    node = element.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _normalize_xml_payload(payload: str) -> str:
    text = payload.lstrip("\ufeff").strip()
    first_tag = text.find("<")
    if first_tag > 0:
        text = text[first_tag:]
    return text


def _sanitize_xml_payload(payload: str) -> str:
    text = _normalize_xml_payload(payload)
    text = re.sub(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD]", "", text)
    text = re.sub(r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]+;)", "&amp;", text)
    return text


def _extract_entsoe_generation_mix(xml_payload: str) -> dict[str, list[float]]:
    normalized_payload = _normalize_xml_payload(xml_payload)
    try:
        root = ET.fromstring(normalized_payload)
    except ET.ParseError as first_error:
        repaired_payload = _sanitize_xml_payload(normalized_payload)
        try:
            root = ET.fromstring(repaired_payload)
        except ET.ParseError:
            raise first_error

    mix: dict[str, list[float]] = {}

    for series in root.findall(".//{*}TimeSeries"):
        psr_code = _xml_text(series, ".//{*}MktPSRType/{*}psrType") or "unknown"
        tech_name = ENTSOE_PSR_TYPE_ALIASES.get(psr_code, psr_code)

        values: list[float] = []
        for period in series.findall(".//{*}Period"):
            points = period.findall(".//{*}Point")
            ordered: list[tuple[int, float]] = []
            for point in points:
                position_text = _xml_text(point, "{*}position")
                quantity_text = _xml_text(point, "{*}quantity")
                if position_text is None or quantity_text is None:
                    continue
                try:
                    position = int(position_text)
                    quantity = float(quantity_text)
                except ValueError:
                    continue
                ordered.append((position, quantity))

            ordered.sort(key=lambda item: item[0])
            values.extend(quantity for _, quantity in ordered)

        if values:
            mix[tech_name] = values[:24]

    return mix


async def _fetch_entsoe_generation_mix(
    client: httpx.AsyncClient,
    period_str: str,
    token: str,
) -> tuple[dict[str, list[float]] | None, dict[str, Any] | None]:
    fetch_started_at = time.perf_counter()
    logger.warning("ENTSO-E fetch started | period=%s", period_str)

    try:
        period_start, period_end = _entsoe_period_bounds(period_str)
        response = await fetch_with_retry(
            client,
            ENTSOE_BASE,
            params={
                "securityToken": token,
                "documentType": ENTSOE_DOCUMENT_TYPE,
                "processType": ENTSOE_PROCESS_TYPE,
                "in_Domain": ENTSOE_DOMAIN_ES,
                "periodStart": period_start,
                "periodEnd": period_end,
            },
            headers={
                "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.1",
                "User-Agent": "energy-mcp/1.0",
            },
            retries=RETRIES,
        )

        content_type = response.headers.get("content-type", "")
        response_text = response.text
        normalized_text = response_text.lstrip().lower()
        if "text/html" in content_type.lower() or normalized_text.startswith("<!doctype html") or normalized_text.startswith("<html"):
            response_preview = _truncate_payload(response_text, limit=350)
            logger.warning(
                "ENTSO-E fetch returned non-XML content | period=%s | content_type=%s | elapsed=%.2f | response_preview=%s",
                period_str,
                content_type,
                time.perf_counter() - fetch_started_at,
                response_preview,
            )
            return None, {
                "code": "unexpected_content_type",
                "message": "ENTSO-E endpoint returned HTML instead of XML",
                "period": period_str,
                "content_type": content_type,
                "response_preview": response_preview,
            }

        mix = _extract_entsoe_generation_mix(response_text)
        if not mix:
            logger.warning(
                "ENTSO-E fetch completed empty | period=%s | elapsed=%.2f",
                period_str,
                time.perf_counter() - fetch_started_at,
            )
            return None, {
                "code": "empty_generation_data",
                "message": "ENTSO-E returned no generation series",
                "period": period_str,
            }
        logger.warning(
            "ENTSO-E fetch succeeded | period=%s | technologies=%s | elapsed=%.2f",
            period_str,
            len(mix),
            time.perf_counter() - fetch_started_at,
        )
        return mix, None
    except httpx.HTTPStatusError as error:
        logger.warning(
            "ENTSO-E fetch http error | period=%s | status=%s | elapsed=%.2f",
            period_str,
            error.response.status_code,
            time.perf_counter() - fetch_started_at,
        )
        return None, {
            "code": "http_error",
            "message": f"ENTSO-E HTTP error: {error}",
            "period": period_str,
            "status_code": error.response.status_code,
        }
    except httpx.RequestError as error:
        request_url = str(error.request.url) if getattr(error, "request", None) else ENTSOE_BASE
        error_message = _format_exception_message(error)
        logger.warning(
            "ENTSO-E fetch network error | period=%s | error=%s | url=%s | elapsed=%.2f",
            period_str,
            error_message,
            request_url,
            time.perf_counter() - fetch_started_at,
        )
        return None, {
            "code": "network_error",
            "message": f"ENTSO-E network error: {error_message}",
            "period": period_str,
            "url": request_url,
        }
    except ET.ParseError as error:
        response_preview = _truncate_payload(response.text if "response" in locals() else "", limit=350)
        logger.warning(
            "ENTSO-E fetch xml parse error | period=%s | error=%s | elapsed=%.2f | response_preview=%s",
            period_str,
            error,
            time.perf_counter() - fetch_started_at,
            response_preview,
        )
        return None, {
            "code": "invalid_xml",
            "message": f"ENTSO-E response XML parse failed: {error}",
            "period": period_str,
            "response_preview": response_preview,
        }
    except Exception as error:
        logger.warning(
            "ENTSO-E fetch failed | period=%s | error=%s | elapsed=%.2f",
            period_str,
            error,
            time.perf_counter() - fetch_started_at,
        )
        return None, {
            "code": "provider_unavailable",
            "message": f"ENTSO-E generation retrieval failed: {error}",
            "period": period_str,
        }


def _compute_carbon_intensity_and_missing(mix_mw: dict[str, float], total_mw: float) -> tuple[float, list[str]]:
    missing_in_run: list[str] = []

    for tech in mix_mw:
        if tech not in EMISSION_FACTORS:
            missing_emission_factors.add(tech)
            missing_in_run.append(tech)

    if total_mw == 0:
        return 0.0, sorted(set(missing_in_run))

    carbon_intensity_estimated = 0.0
    for tech, mw in mix_mw.items():
        factor = EMISSION_FACTORS.get(tech, 0.0)
        carbon_intensity_estimated += (mw / total_mw) * factor

    return carbon_intensity_estimated, sorted(set(missing_in_run))


def _build_generation_analytics(
    date_str: str,
    mix_raw: dict[str, Any],
    endpoint_used: str | None = None,
    days_fallback: int = 0,
) -> dict[str, Any]:
    """
    Build generation analytics with typed numeric maps.
    Returns:
        {
            'date': str,
            'mix_mw': dict[str, float],
            'mix_pct': dict[str, float],
            'indicators': dict[str, float | bool | int]
        }
    """
    missing_emission_factors.clear()
    mix: dict[str, float] = {}
    all_series_have_24_values = 1

    for tech_name, series in mix_raw.items():
        canonical_name = _canonical_tech_name(tech_name)
        if canonical_name in TOTAL_TECH_KEYS:
            continue

        series_values = series if isinstance(series, list) else [series]
        normalized_series = _normalize_series_to_24(series_values)
        if not normalized_series:
            all_series_have_24_values = 0
            continue
        if len(series_values) != 24:
            all_series_have_24_values = 0

        mw = _series_to_mw(normalized_series)
        mix[canonical_name] = mix.get(canonical_name, 0.0) + mw

    mix_mw = {tech: round(mw, 4) for tech, mw in mix.items()}
    total_mw = round(sum(mix_mw.values()), 4)

    if total_mw == 0:
        mix_pct = {tech: 0.0 for tech in mix_mw}
    else:
        mix_pct = {
            tech: round((mw / total_mw) * 100, 2)
            for tech, mw in mix_mw.items()
        }

    renewable_mw = round(
        sum(mix_mw.get(tech, 0.0) for tech in RENEWABLE_TECH),
        4,
    )
    non_renewable_mw = round(
        sum(mix_mw.get(tech, 0.0) for tech in NON_RENEWABLE_TECH),
        4,
    )

    if total_mw == 0:
        renewable_share = 0.0
        gas_share = 0.0
        nuclear_share = 0.0
        thermal_share = 0.0
    else:
        renewable_share = renewable_mw / total_mw
        gas_share = mix_mw.get("combined cycle", 0.0) / total_mw
        nuclear_share = mix_mw.get("nuclear", 0.0) / total_mw
        thermal_share = (
            mix_mw.get("combined cycle", 0.0)
            + mix_mw.get("coal", 0.0)
            + mix_mw.get("cogeneration", 0.0)
        ) / total_mw

    carbon_intensity_estimated, missing_for_result = _compute_carbon_intensity_and_missing(
        mix_mw,
        total_mw,
    )

    indicators: dict[str, Any] = {
        "renewable_share": round(renewable_share, 4),
        "gas_dependency": round(gas_share, 4),
        "nuclear_share": round(nuclear_share, 4),
        "thermal_share": round(thermal_share, 4),
        "carbon_intensity_estimated": round(carbon_intensity_estimated, 4),
        "defaults_applied": len(missing_for_result) > 0,
        "_has_exactly_24_values": all_series_have_24_values,
    }
    if missing_for_result:
        indicators["missing_emission_factors"] = missing_for_result

    generation_by_type = {
        "renewable": {
            "mw": renewable_mw,
            "share": round(renewable_share, 4),
        },
        "non_renewable": {
            "mw": non_renewable_mw,
            "share": round(1 - renewable_share, 4) if total_mw else 0.0,
        },
    }

    logger.info(
        "date=%s endpoint_used=%s days_fallback=%s renewable_share=%s gas_dependency=%s nuclear_share=%s thermal_share=%s",
        date_str,
        endpoint_used or "none",
        days_fallback,
        indicators["renewable_share"],
        indicators["gas_dependency"],
        indicators["nuclear_share"],
        indicators["thermal_share"],
    )

    return {
        "date": date_str,
        "total_mw": total_mw,
        "mix_mw": mix_mw,
        "mix_pct": mix_pct,
        "generation_by_type": generation_by_type,
        "classified": {
            "renewable": {
                "mw": renewable_mw,
                "share": round(renewable_share, 4),
            },
            "non_renewable": {
                "mw": non_renewable_mw,
                "share": round(1 - renewable_share, 4) if total_mw else 0.0,
            },
        },
        "indicators": indicators,
        "meta": {
            "endpoint_used": endpoint_used,
            "days_fallback": days_fallback,
        },
    }

# Tools
@mcp.tool()
async def load_consumption_data(period: str) -> dict:
    """
    Devuelve consumo eléctrico real por hora para un día específico (period = 'YYYY-MM-DD').
    """
    # validar formato YYYY-MM-DD
    try:
        period_date = _parse_period_date(period)
    except Exception as e:
        return _error_response(
            "invalid_period",
            f"invalid period format: {e}",
            "load_consumption_data",
            "ree",
            False,
        )

    # REE sólo devuelve histórico cerrado: usar fecha pasada (no hoy ni futuro)
    today = _utc_today()
    used_date = period_date
    adjusted_date = False
    if period_date >= today:
        used_date = today - timedelta(days=1)
        adjusted_date = True

    used_period = used_date.strftime("%Y-%m-%d")
    staleness_days = (period_date - used_date).days

    url = f"{REE_BASE}/demanda/demanda-tiempo-real"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await fetch_with_retry(
                client,
                url,
                params=_ree_day_params(used_period, time_trunc="hour"),
            )
            try:
                data = response.json()
            except json.JSONDecodeError:
                return _error_response(
                    "invalid_json",
                    "Invalid JSON in consumption provider response",
                    "load_consumption_data",
                    "ree",
                    False,
                    extra={"endpoint": "demanda-tiempo-real", "period": used_period},
                )

        included = data.get("included", [])
        if not isinstance(included, list) or not included:
            logger.error("Consumption load failed: %s", _truncate_payload(data))
            return _error_response(
                "malformed_payload",
                "consumption load failed: truncated payload",
                "load_consumption_data",
                "ree",
                False,
            )

        actual_curve = next((item for item in included if _is_actual_demand_curve(item)), None)
        if actual_curve is None:
            available_curves = [str(item.get("type", "unknown")) for item in included if isinstance(item, dict)]
            return _error_response(
                "actual_curve_not_found",
                "actual demand curve not found in API response",
                "load_consumption_data",
                "ree",
                False,
                extra={"available_curves": available_curves},
            )

        values = actual_curve.get("attributes", {}).get("values", [])
        if not isinstance(values, list):
            logger.error("Consumption load failed: %s", _truncate_payload(actual_curve))
            return _error_response(
                "malformed_payload",
                "consumption load failed: truncated payload",
                "load_consumption_data",
                "ree",
                False,
            )

        logger.info("Actual demand raw points received: %s", len(values))
        consumption = _to_hourly_consumption(values)
        return {
            "period": used_period,
            "consumption": consumption,
            "adjusted_date": adjusted_date,
            "original_date": period,
            "used_date": used_period,
            "requested_date": period,
            "effective_date": used_period,
            "data_staleness_days": staleness_days,
        }
    except httpx.HTTPStatusError as e:
        return _error_response(
            "http_error",
            f"HTTP error: {e}",
            "load_consumption_data",
            "ree",
            e.response.status_code >= 500,
        )
    except ValueError as e:
        return _error_response(
            "invalid_data",
            str(e),
            "load_consumption_data",
            "ree",
            False,
        )
    except Exception as e:
        logger.error("Consumption load failed: %s", _truncate_payload(e))
        return _error_response(
            "internal_error",
            "consumption load failed: truncated payload",
            "load_consumption_data",
            "ree",
            False,
        )

@mcp.tool()
async def load_weather_data(location: str, period: str) -> dict:
    """
    Devuelve datos meteorológicos reales para la ubicación y día indicados.
    period: 'YYYY-MM-DD'
    """
    try:
        datetime.strptime(period, "%Y-%m-%d")
    except Exception as e:
        return _error_response(
            "invalid_period",
            f"invalid period format: {e}",
            "load_weather_data",
            "meteostat",
            False,
        )

    location_query = location.split(",")[0].strip() if location else ""
    if not location_query:
        return _error_response(
            "invalid_location",
            "invalid location",
            "load_weather_data",
            "meteostat",
            False,
        )

    _load_env_file()
    api_key = os.getenv("METEOSTAT_API_KEY")
    if not api_key:
        return _error_response(
            "internal_error",
            "METEOSTAT_API_KEY missing",
            "load_weather_data",
            "meteostat",
            False,
        )

    base_url = "https://meteostat.p.rapidapi.com"
    weather_url = f"{base_url}/point/hourly"
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": "meteostat.p.rapidapi.com",
    }

    lat = 40.4168
    lon = -3.7038
    parts = [p.strip() for p in location.split(",")] if location else []
    if len(parts) >= 2:
        try:
            lat = float(parts[0])
            lon = float(parts[1])
        except ValueError:
            pass

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            weather_resp = await fetch_with_retry(
                client,
                weather_url,
                params={
                    "lat": lat,
                    "lon": lon,
                    "start": period,
                    "end": period,
                },
            )
            try:
                weather_json = weather_resp.json()
            except json.JSONDecodeError:
                return _error_response(
                    "invalid_json",
                    "Invalid JSON in weather provider response",
                    "load_weather_data",
                    "meteostat",
                    False,
                    extra={"endpoint": "point/hourly", "period": period},
                )

        hourly = weather_json.get("data", [])
        if not isinstance(hourly, list):
            logger.error("Weather load failed: %s", _truncate_payload(weather_json))
            return _error_response(
                "malformed_payload",
                "weather load failed: truncated payload",
                "load_weather_data",
                "meteostat",
                False,
            )

        temperatures = [entry.get("temp") for entry in hourly if isinstance(entry, dict)]
        wind_speeds = [entry.get("wspd") for entry in hourly if isinstance(entry, dict)]

        solar_candidates = [
            "radiation",
            "shortwave",
            "shortwave_radiation",
            "solar_radiation",
            "ghi",
            "tsun",
        ]

        def pick_solar_value(entry: dict[str, Any]) -> Any:
            for key in solar_candidates:
                if key in entry and entry.get(key) is not None:
                    return entry.get(key)
            return None

        solar = [pick_solar_value(entry) for entry in hourly if isinstance(entry, dict)]

        if hourly and isinstance(hourly[0], dict):
            first_keys = set(hourly[0].keys())
            if not any(key in first_keys for key in solar_candidates):
                logger.warning(
                    "Meteostat solar field not found; available keys=%s",
                    sorted(first_keys),
                )

        if not all(isinstance(series, list) for series in [temperatures, wind_speeds, solar]):
            logger.error("Weather load failed: %s", _truncate_payload(weather_json))
            return _error_response(
                "malformed_payload",
                "weather load failed: truncated payload",
                "load_weather_data",
                "meteostat",
                False,
            )

        weather_series = {
            "temperature": temperatures,
            "wind_speed": wind_speeds,
            "solar_irradiance": solar,
        }

        invalid_series = [
            name
            for name, series in weather_series.items()
            if _series_24_indicator(series, use_nan=False) != 1
        ]
        if invalid_series:
            return _error_response(
                "invalid_weather_series",
                "weather series must contain exactly 24 hourly values",
                "load_weather_data",
                "meteostat",
                False,
                extra={
                    "invalid_series": invalid_series,
                    "lengths": {name: len(series) for name, series in weather_series.items()},
                },
            )

        logger.info(
            "Weather series lengths | temperature=%s | wind_speed=%s | solar_irradiance=%s",
            len(weather_series["temperature"]),
            len(weather_series["wind_speed"]),
            len(weather_series["solar_irradiance"]),
        )

        return {
            "location": location,
            "period": period,
            "latitude": lat,
            "longitude": lon,
            "temperature": weather_series["temperature"],
            "wind_speed": weather_series["wind_speed"],
            "solar_irradiance": weather_series["solar_irradiance"],
        }
    except httpx.HTTPStatusError as e:
        return _error_response(
            "http_error",
            f"HTTP error: {e}",
            "load_weather_data",
            "meteostat",
            e.response.status_code >= 500,
        )
    except Exception as e:
        logger.error("Weather load failed: %s", _truncate_payload(e))
        return _error_response(
            "internal_error",
            "weather load failed: truncated payload",
            "load_weather_data",
            "meteostat",
            False,
        )


@mcp.tool()
async def get_daily_energy_context(period: str, location: str) -> dict:
    """
    Devuelve un contexto energético diario unificado:
    demanda, clima y mix de generación.

    Output:
    {
        "period": "YYYY-MM-DD",
        "location": "City,CC",
        "demand": [int, ...],
        "weather": {
            "latitude": float,
            "longitude": float,
            "temperature": [float x24],
            "wind_speed": [float x24],
            "solar_irradiance": [float x24]
        },
        "generation_mix": {...}
    }
    """
    consumption_task = load_consumption_data(period)
    weather_task = load_weather_data(location, period)
    generation_task = get_generation_mix(period)

    consumption_result, weather_result, generation_result = await asyncio.gather(
        consumption_task,
        weather_task,
        generation_task,
    )

    demand = None if _has_tool_error(consumption_result) else consumption_result.get("consumption", [])
    weather = None
    if not _has_tool_error(weather_result):
        weather = {
            "latitude": weather_result.get("latitude"),
            "longitude": weather_result.get("longitude"),
            "temperature": weather_result.get("temperature", []),
            "wind_speed": weather_result.get("wind_speed", []),
            "solar_irradiance": weather_result.get("solar_irradiance", []),
        }
    generation_mix = None if _has_tool_error(generation_result) else generation_result

    errors: dict[str, Any] = {}
    if _has_tool_error(consumption_result):
        errors["demand"] = consumption_result
    if _has_tool_error(weather_result):
        errors["weather"] = weather_result
    if _has_tool_error(generation_result):
        errors["generation_mix"] = generation_result

    partial_failure = bool(errors)
    if partial_failure:
        logger.warning(
            "Daily context partial failure | period=%s | location=%s | failed_components=%s",
            period,
            location,
            sorted(errors.keys()),
        )
    else:
        logger.info("Daily energy context complete | period=%s | location=%s", period, location)

    return {
        "period": period,
        "location": location,
        "demand": demand,
        "weather": weather,
        "generation_mix": generation_mix,
        "partial_failure": partial_failure,
        "errors": errors,
    }


@mcp.tool()
async def summarize_daily_energy_context(period: str, location: str) -> dict:
    """
    Devuelve un resumen en lenguaje natural del contexto energético diario.
    """
    context = await get_daily_energy_context(period, location)
    if context.get("demand") is None and context.get("weather") is None and context.get("generation_mix") is None:
        return _error_response(
            "context_unavailable",
            "failed to build daily energy context",
            "summarize_daily_energy_context",
            "energy-server",
            True,
            extra={"period": period, "location": location, "details": context.get("errors", {})},
        )

    prompt = (
        "Summarize this daily energy context in 4 short bullet points. "
        "Use plain language.\n"
        f"{json.dumps(context, ensure_ascii=False)}"
    )
    model_name = _get_model_name()
    llm_result = await _query_llm(prompt, model_name=model_name)
    if _has_tool_error(llm_result):
        return llm_result

    return {
        "period": period,
        "location": location,
        "model_name": model_name,
        "summary": llm_result.get("content", ""),
    }


@mcp.tool()
async def get_tool_schemas() -> dict:
    """
    Devuelve documentación formal de salida (schema-like) para las tools.
    """
    return TOOL_RESPONSE_SCHEMAS

@mcp.tool()
async def get_generation_mix(period: str) -> dict:
    """
    Devuelve contexto energético estratégico para un día específico,
    incluyendo mix en MW, porcentajes, clasificación renovable/no renovable
    e indicadores clave para análisis y decisión.
    """
    try:
        requested = _parse_period_date(period)
    except ValueError as e:
        return _error_response(
            "invalid_period",
            f"invalid period format: {e}",
            "get_generation_mix",
            "entsoe",
            False,
        )

    _load_env_file()
    entsoe_api_key = os.getenv("ENTSOE_API_KEY")
    if not entsoe_api_key:
        return _error_response(
            "provider_unavailable",
            "ENTSOE_API_KEY missing",
            "get_generation_mix",
            "entsoe",
            False,
        )

    max_date = _utc_today() - timedelta(days=3)
    target = min(requested, max_date)

    last_error: dict[str, Any] | None = None

    async with httpx.AsyncClient(timeout=GENERATION_TIMEOUT) as client:
        for i in range(GEN_FALLBACK_DAYS):
            test_date = target - timedelta(days=i)
            period_str = test_date.strftime("%Y-%m-%d")
            fallback_used = i > 0

            mix, error = await _fetch_entsoe_generation_mix(
                client,
                period_str,
                entsoe_api_key,
            )

            if mix:
                if fallback_used:
                    logger.info(
                        "Generation fallback succeeded | requested=%s | effective=%s | provider=entsoe",
                        period,
                        period_str,
                    )
                analytics = _build_generation_analytics(
                    period_str,
                    mix,
                    endpoint_used="entsoe-a75",
                    days_fallback=i,
                )
                analytics["requested_date"] = period
                analytics["effective_date"] = period_str
                analytics["data_staleness_days"] = (requested - test_date).days
                return analytics

            last_error = error
            logger.warning(
                "Generation provider failure; trying fallback day | requested=%s | effective=%s | details=%s",
                period,
                period_str,
                error,
            )

    return _error_response(
        "provider_unavailable",
        "ENTSO-E generation endpoint unavailable after retries/fallback",
        "get_generation_mix",
        "entsoe",
        True,
        extra={
            "period_requested": period,
            "endpoint_last_attempted": "entsoe-a75",
            "fallback_attempted": True,
            "details": last_error if last_error else "no data returned",
        },
    )

# Recursos
@mcp.resource("energy://consumption/{period}")
async def read_consumption(period: str) -> dict:
    return await load_consumption_data(period)

@mcp.resource("energy://generation/{period}")
async def read_generation(period: str) -> dict:
    return await get_generation_mix(period)


@mcp.resource("energy://context/{period}/{location}")
async def read_daily_context(period: str, location: str) -> dict:
    return await get_daily_energy_context(period, location)


@mcp.resource("energy://context-summary/{period}/{location}")
async def read_daily_context_summary(period: str, location: str) -> dict:
    return await summarize_daily_energy_context(period, location)


@mcp.resource("energy://schemas")
async def read_tool_schemas() -> dict:
    return await get_tool_schemas()

if __name__ == "__main__":
    mcp.run(transport='stdio')
