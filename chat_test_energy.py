import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Try importing rich for premium CLI styling
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.box import ROUNDED
    console = Console()
except ImportError:
    # Fallback to standard prints if rich is not present
    class ConsoleFallback:
        def print(self, *args, **kwargs):
            print(*args)
        def input(self, prompt):
            return input(prompt)
    console = ConsoleFallback()
    Panel = Text = Syntax = Table = ROUNDED = None

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
        console.print("[bold red][ERROR] Falta la variable de entorno OPENAI_API_KEY en tu .env[/bold red]")
        return

    # Render a beautiful header if rich is available
    if Panel:
        welcome_text = Text()
        welcome_text.append("⚡ mcp-SADE-ME ⚡\n", style="bold green")
        welcome_text.append("Smart Energy AI Agent CLI\n\n", style="italic cyan")
        welcome_text.append(f"• LLM Model: {MODEL_NAME}\n", style="yellow")
        welcome_text.append("• Server Mode: Local FastMCP (server_data2.py)\n", style="yellow")
        welcome_text.append("• Type exit/quit/q to close.\n\n", style="dim")
        welcome_text.append("💡 NL Prompt: Type anything to chat with the smart energy assistant.\n", style="green")
        welcome_text.append("🔧 Direct Tool: Use '/tool <name> <json_args>' to invoke tools directly.", style="magenta")
        
        console.print(Panel(welcome_text, border_style="green", box=ROUNDED, title="🤖 Agent Interface Active"))
    else:
        print(f"Ready. model={MODEL_NAME}. Type exit/quit/q.")
        print("Direct tool mode: /tool <tool_name> <json_args>")

    server_path = Path(__file__).parent / "mcp_servers" / "energy_data" / "server_data2.py"
    params = {"command": sys.executable, "args": ["-u", str(server_path)]}

    async with MCPServerStdio(params=params, client_session_timeout_seconds=120) as mcp_server:
        while True:
            try:
                if Panel:
                    msg = console.input("\n[bold green]You ➔[/bold green] ").strip()
                else:
                    msg = input("\nYou: ").strip()
            except (KeyboardInterrupt, EOFError):
                break

            if not msg:
                continue
            if msg.lower() in EXIT:
                break

            parsed = _parse_tool_command(msg)
            if parsed is not None:
                tool_name, arguments = parsed
                console.print(f"\n[bold yellow]🔧 Ejecutando herramienta directa:[/bold yellow] [bold cyan]{tool_name}[/bold cyan]...")
                try:
                    tool_result = await mcp_server.call_tool(tool_name, arguments)
                    if Syntax:
                        formatted_json = json.dumps(tool_result, indent=2, ensure_ascii=False)
                        syntax_block = Syntax(formatted_json, "json", theme="monokai", word_wrap=True)
                        console.print(Panel(syntax_block, title=f"Result: {tool_name}", border_style="blue"))
                    else:
                        print(f"\nResult ({tool_name}): {tool_result}")
                except Exception as e:
                    console.print(f"\n[bold red]❌ Error de herramienta ({tool_name}):[/bold red] {e}")
                continue

            # Run with OpenAI Agent
            console.print("\n[bold yellow]🤖 Pensando...[/bold yellow]")
            try:
                result = await Runner.run(agent, msg, max_turns=4)
                if Panel:
                    console.print(Panel(result.final_output, title="🤖 Assistant Response", border_style="cyan"))
                else:
                    print(f"\nAssistant: {result.final_output}")
            except Exception as e:
                console.print(f"\n[bold red]❌ Assistant Error:[/bold red] {e}")

if __name__ == "__main__":
    # We must construct the agent here because 'agent' is referenced inside main()
    # Note: we need model client loader to pass down to Agent
    from mcp_servers.energy_data.server_data2 import get_wrapped_model
    wrapped_model = get_wrapped_model(MODEL_NAME)
    
    agent = Agent(
        name="energy_chat_tester",
        instructions=(
            "Use MCP tools from server_data2.py to answer user questions. "
            "Avoid repeated retries on the same tool call; if a tool fails, explain the error and stop."
        ),
        model=wrapped_model,
    )
    asyncio.run(main())