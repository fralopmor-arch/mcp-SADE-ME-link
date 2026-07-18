import asyncio
import json
import os
import sys
from typing import Any

# Try importing rich for visual enhancement
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.syntax import Syntax
    from rich.box import ROUNDED
    console = Console()
except ImportError:
    class ConsoleFallback:
        def print(self, *args, **kwargs):
            print(*args)
        def input(self, prompt):
            return input(prompt)
    console = ConsoleFallback()
    Panel = Text = Syntax = ROUNDED = None

from mcp_servers.energy_data.server_data2 import (
    get_daily_energy_context,
    get_generation_mix,
    load_consumption_data,
    load_weather_data,
    summarize_daily_energy_context,
)


DEFAULT_SMOKE_PROMPTS = {
    "load_consumption_data": "show full energy context",
    "load_weather_data": "show full energy context",
    "get_generation_mix": "show full energy context",
    "get_daily_energy_context": "show full energy context",
    "summarize_daily_energy_context": "summary this",
}


def parse_intent(prompt: str) -> str:
    text = prompt.lower()
    if "summary" in text or "summarize" in text or "resumen" in text:
        return "summary"
    return "raw context"


def _error_message(error_value: Any) -> str:
    if isinstance(error_value, str):
        return error_value
    if isinstance(error_value, dict):
        if isinstance(error_value.get("message"), str):
            return error_value["message"]
        if isinstance(error_value.get("error"), str):
            return error_value["error"]
    return "unknown error"


def _normalize_result(result: Any, source_name: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"error": f"{source_name} must return dict"}
    if "error" in result:
        return {"error": _error_message(result["error"])}
    return result


async def _call_tool(source_name: str, coro: Any) -> dict[str, Any]:
    try:
        raw_result = await coro
    except Exception as exc:
        return {"error": str(exc)}
    return _normalize_result(raw_result, source_name)


async def run_chat_action(prompt: str, period: str, location: str) -> dict[str, Any]:
    intent = parse_intent(prompt)
    context = await _call_tool(
        "get_daily_energy_context", get_daily_energy_context(period, location)
    )
    if "error" in context:
        return context

    if intent == "summary":
        summary_result = await _call_tool(
            "summarize_daily_energy_context",
            summarize_daily_energy_context(period, location),
        )
        if "error" in summary_result:
            return summary_result

        return {
            "intent": "summary",
            "period": period,
            "location": location,
            "context": context,
            "summary": summary_result.get("summary", ""),
        }

    return {
        "intent": "raw context",
        "period": period,
        "location": location,
        "context": context,
    }


def _observed_from_result(result: dict[str, Any]) -> str:
    if isinstance(result, dict) and "error" in result:
        return "fail"
    return "pass"


async def run_smoke_flow(period: str, location: str) -> dict[str, Any]:
    raw_prompt = DEFAULT_SMOKE_PROMPTS["get_daily_energy_context"]
    summary_prompt = DEFAULT_SMOKE_PROMPTS["summarize_daily_energy_context"]

    consumption_result = await _call_tool(
        "load_consumption_data", load_consumption_data(period)
    )
    weather_result = await _call_tool("load_weather_data", load_weather_data(location, period))
    generation_result = await _call_tool("get_generation_mix", get_generation_mix(period))
    raw_result = await run_chat_action(raw_prompt, period, location)
    summary_result = await run_chat_action(summary_prompt, period, location)

    mapping = [
        {
            "tool_or_function": "load_consumption_data",
            "prompt_used": DEFAULT_SMOKE_PROMPTS["load_consumption_data"],
            "observed_result": _observed_from_result(consumption_result),
        },
        {
            "tool_or_function": "load_weather_data",
            "prompt_used": DEFAULT_SMOKE_PROMPTS["load_weather_data"],
            "observed_result": _observed_from_result(weather_result),
        },
        {
            "tool_or_function": "get_generation_mix",
            "prompt_used": DEFAULT_SMOKE_PROMPTS["get_generation_mix"],
            "observed_result": _observed_from_result(generation_result),
        },
        {
            "tool_or_function": "get_daily_energy_context",
            "prompt_used": raw_prompt,
            "observed_result": _observed_from_result(raw_result),
        },
        {
            "tool_or_function": "summarize_daily_energy_context",
            "prompt_used": summary_prompt,
            "observed_result": _observed_from_result(summary_result),
        },
    ]

    return {
        "period": period,
        "location": location,
        "mapping": mapping,
        "raw_result": raw_result,
        "summary_result": summary_result,
    }


def main() -> None:
    if Panel:
        welcome_text = Text()
        welcome_text.append("⚡ SADE-ME Local Data Runner ⚡\n", style="bold green")
        welcome_text.append("Query live energy data directly from local APIs\n", style="italic dim cyan")
        console.print(Panel(welcome_text, border_style="cyan", box=ROUNDED))
    else:
        print("--- SADE-ME Local Data Runner ---")

    if Panel:
        period = console.input("[bold yellow]📅 Ingrese la fecha (YYYY-MM-DD):[/bold yellow] ").strip()
        location = console.input("[bold yellow]📍 Ingrese la ubicación (Ciudad,Pais):[/bold yellow] ").strip()
    else:
        period = input("period (YYYY-MM-DD): ").strip()
        location = input("location (City,CC): ").strip()

    if Panel:
        console.print("\n[bold green]Escribe un prompt. Ejemplos:[/bold green]")
        console.print("  • 'resumen' o 'summary' (genera un informe usando OpenAI)")
        console.print("  • 'datos' o 'raw' (muestra el JSON completo de demanda/clima/mix)")
        console.print("  • Escribe 'exit' para salir.")
    else:
        print("Type a prompt. Use 'exit' to quit.")

    while True:
        try:
            if Panel:
                prompt = console.input("\n[bold cyan]query ➔[/bold cyan] ").strip()
            else:
                prompt = input("chat> ").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit"}:
            break

        if Panel:
            console.print("[bold yellow]⏳ Recuperando datos y procesando consulta...[/bold yellow]")
        
        result = asyncio.run(run_chat_action(prompt, period, location))

        if Syntax:
            formatted_json = json.dumps(result, indent=2, ensure_ascii=False)
            syntax_block = Syntax(formatted_json, "json", theme="monokai", word_wrap=True)
            console.print(Panel(syntax_block, title="Query Response JSON", border_style="green"))
        else:
            print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()