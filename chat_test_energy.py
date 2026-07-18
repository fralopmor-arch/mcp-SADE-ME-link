import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
try:
    from agents import Agent, Runner
    from agents.mcp import MCPServerStdio
except ImportError as e:
    print("\n[ERROR] No se pudo importar la librería 'agents'.")
    print("Asegúrate de haber activado el entorno virtual (.venv) e instalado las dependencias:")
    print("  1. Activar entorno virtual: .venv\\Scripts\\Activate.ps1")
    print("  2. Instalar dependencias:   pip install -r requirements.txt")
    print(f"Detalle del error original: {e}\n")
    sys.exit(1)

load_dotenv(override=True)

MODEL_NAME = os.getenv("ENERGY_CHAT_MODEL", "gpt-4o-mini")
EXIT = {"exit", "quit", "q"}


def _parse_tool_command(message: str) -> tuple[str, dict] | None:
    parts = message.strip().split(" ", 2)
    if len(parts) < 2 or parts[0] != "/tool":
        return None

    tool_name = parts[1].strip()
    if not tool_name:
        return None

    if len(parts) == 2:
        return tool_name, {}

    try:
        payload = json.loads(parts[2])
        if not isinstance(payload, dict):
            return None
    except json.JSONDecodeError:
        return None

    return tool_name, payload

async def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("Missing OPENAI_API_KEY in env/.env")
        return

    server_path = Path(__file__).parent / "mcp_servers" / "energy_data" / "server_data2.py"
    params = {"command": sys.executable, "args": ["-u", str(server_path)]}

    async with MCPServerStdio(params=params, client_session_timeout_seconds=120) as mcp_server:
        agent = Agent(
            name="energy_chat_tester",
            instructions=(
                "Use MCP tools from server_data2.py to answer user questions. "
                "Avoid repeated retries on the same tool call; if a tool fails, explain the error and stop."
            ),
            model=MODEL_NAME,
            mcp_servers=[mcp_server],
        )

        print(f"Ready. model={MODEL_NAME}. Type exit/quit/q.")
        print("Direct tool mode: /tool <tool_name> <json_args>")
        print("Example: /tool get_generation_mix {\"period\":\"2026-02-14\"}")
        while True:
            msg = input("\nYou: ").strip()
            if not msg:
                continue
            if msg.lower() in EXIT:
                break

            parsed = _parse_tool_command(msg)
            if parsed is not None:
                tool_name, arguments = parsed
                try:
                    tool_result = await mcp_server.call_tool(tool_name, arguments)
                    print(f"\nTool Result ({tool_name}): {tool_result}")
                except Exception as e:
                    print(f"\nTool Error ({tool_name}): {e}")
                continue

            try:
                result = await Runner.run(agent, msg, max_turns=4)
                print(f"\nAssistant: {result.final_output}")
            except Exception as e:
                print(f"\nAssistant Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())