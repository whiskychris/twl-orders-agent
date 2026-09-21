"""Run the model with the tools the caller's roles allow."""

import os

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .anthropic_auth import get_anthropic_token
from .tools import SERVER_NAME, build_server

MODEL = "claude-sonnet-5"
MAX_TURNS = 10
MAX_BUDGET_USD = 0.50


async def run_agent(prompt, ctx, state=None):
    """ctx is the caller's AuthContext. It decides which tools exist for this request. `state` collects
    what the tools produced (a prepared draft order), which main.py posts instead of the model's words."""
    token = get_anthropic_token()
    server, tool_names = build_server(ctx, state)

    options = ClaudeAgentOptions(
        model=MODEL,
        cwd=os.getcwd(),
        setting_sources=["project"],  # loads CLAUDE.md, the agent's instructions and hard rules
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[f"mcp__{SERVER_NAME}__{name}" for name in tool_names],
        max_turns=MAX_TURNS,
        max_budget_usd=MAX_BUDGET_USD,
        env={"ANTHROPIC_AUTH_TOKEN": token},
    )

    final_result = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            if message.is_error:
                raise RuntimeError(message.result or "The agent returned an error")
            final_result = message.result
    return final_result or ""
