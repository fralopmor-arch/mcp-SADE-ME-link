"""Local MCP runner.

Observability notes source:
- mcp_servers.energy_data.server_data2.OBSERVABILITY_CHECKLIST
- mcp_servers.energy_data.server_data2.GENERATION_ENDPOINT_LOG_CONTRACT
- mcp_servers.energy_data.server_data2.GENERATION_ANALYTICS_PAYLOAD_CONTRACT
- mcp_servers.energy_data.server_data2.MCP_RESPONSE_NOTES

Transport remains stdio (no runtime behavior changes).
"""

import logging
from mcp_servers.energy_data.server_data2 import mcp

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    logger.info("Arrancando mcp (transport=stdio)...")
    mcp.run(transport="stdio")
