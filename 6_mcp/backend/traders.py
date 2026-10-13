from contextlib import AsyncExitStack
from .accounts_client import read_accounts_resource, read_strategy_resource
from .tracers import make_trace_id
from agents import Agent, Tool, Runner, OpenAIChatCompletionsModel, trace
from openai import AsyncOpenAI
from dotenv import load_dotenv
import os
import json
from .accounts import SHORT_TRADERS
from .templates import (
    researcher_instructions,
    trader_instructions,
    trade_message,
    rebalance_message,
    research_tool,
    aggressive_trader_instructions,
    aggressive_trade_message,
    aggressive_rebalance_message,
)
from .mcp_servers import trader_mcp_servers, researcher_mcp_servers

load_dotenv(override=True)

deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
google_api_key = os.getenv("GOOGLE_API_KEY")
grok_api_key = os.getenv("GROK_API_KEY")
openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
GROK_BASE_URL = "https://api.x.ai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

MAX_TURNS = 30
RECENT_TRADES = 5


def brief_account(account_json: dict) -> dict:
    """The slice of an account the model needs to trade.

    Strategy is omitted because the user message already carries it. The ledger
    stays in the database; only the latest trades go into the prompt.
    """
    trades = account_json.get("transactions") or []
    recent = trades[-RECENT_TRADES:]
    return {
        "balance": account_json.get("balance"),
        "holdings": account_json.get("holdings") or {},
        "exits": account_json.get("exits") or {},
        "recent_trades": recent,
        "earlier_trade_count": max(0, len(trades) - len(recent)),
        "total_portfolio_value": account_json.get("total_portfolio_value"),
        "total_profit_loss": account_json.get("total_profit_loss"),
    }

def _client(base_url: str, api_key: str | None) -> AsyncOpenAI:
    # The SDK rejects a missing key at construction. Unused providers get a
    # placeholder so the floor can start on whichever key is actually set.
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "missing")


openrouter_client = _client(OPENROUTER_BASE_URL, openrouter_api_key)
deepseek_client = _client(DEEPSEEK_BASE_URL, deepseek_api_key)
grok_client = _client(GROK_BASE_URL, grok_api_key)
gemini_client = _client(GEMINI_BASE_URL, google_api_key)


def get_model(model_name: str):
    if "/" in model_name:
        return OpenAIChatCompletionsModel(model=model_name, openai_client=openrouter_client)
    elif "deepseek" in model_name:
        return OpenAIChatCompletionsModel(model=model_name, openai_client=deepseek_client)
    elif "grok" in model_name:
        return OpenAIChatCompletionsModel(model=model_name, openai_client=grok_client)
    elif "gemini" in model_name:
        return OpenAIChatCompletionsModel(model=model_name, openai_client=gemini_client)
    else:
        return model_name


async def get_researcher(mcp_servers, model_name) -> Agent:
    researcher = Agent(
        name="Researcher",
        instructions=researcher_instructions(),
        model=get_model(model_name),
        mcp_servers=mcp_servers,
    )
    return researcher


async def get_researcher_tool(mcp_servers, model_name) -> Tool:
    researcher = await get_researcher(mcp_servers, model_name)
    return researcher.as_tool(tool_name="Researcher", tool_description=research_tool())


class Trader:
    def __init__(self, name: str, lastname="Trader", model_name="deepseek-v4-flash"):
        self.name = name
        self.lastname = lastname
        self.agent = None
        self.model_name = model_name
        self.do_trade = True

    async def create_agent(self, trader_mcp_servers, researcher_mcp_servers) -> Agent:
        tool = await get_researcher_tool(researcher_mcp_servers, self.model_name)
        instructions = (
            aggressive_trader_instructions(self.name)
            if self.name.lower() in SHORT_TRADERS
            else trader_instructions(self.name)
        )
        self.agent = Agent(
            name=self.name,
            instructions=instructions,
            model=get_model(self.model_name),
            tools=[tool],
            mcp_servers=trader_mcp_servers,
        )
        return self.agent

    async def get_account_report(self) -> str:
        account = await read_accounts_resource(self.name)
        return json.dumps(brief_account(json.loads(account)))

    async def run_agent(self, trader_mcp_servers, researcher_mcp_servers):
        self.agent = await self.create_agent(trader_mcp_servers, researcher_mcp_servers)
        account = await self.get_account_report()
        strategy = await read_strategy_resource(self.name)
        if self.name.lower() in SHORT_TRADERS:
            message = (
                aggressive_trade_message(self.name, strategy, account)
                if self.do_trade
                else aggressive_rebalance_message(self.name, strategy, account)
            )
        else:
            message = (
                trade_message(self.name, strategy, account)
                if self.do_trade
                else rebalance_message(self.name, strategy, account)
            )
        await Runner.run(self.agent, message, max_turns=MAX_TURNS)

    async def run_with_mcp_servers(self):
        async with AsyncExitStack() as stack:
            trader_servers = [
                await stack.enter_async_context(server) for server in trader_mcp_servers(self.name)
            ]
            researcher_servers = [
                await stack.enter_async_context(server)
                for server in researcher_mcp_servers(self.name)
            ]
            await self.run_agent(trader_servers, researcher_servers)

    async def run_with_trace(self):
        trace_name = f"{self.name}-trading" if self.do_trade else f"{self.name}-rebalancing"
        trace_id = make_trace_id(f"{self.name.lower()}")
        with trace(trace_name, trace_id=trace_id):
            await self.run_with_mcp_servers()

    async def run(self):
        try:
            await self.run_with_trace()
        except Exception as e:
            print(f"Error running trader {self.name}: {e}")
        self.do_trade = not self.do_trade
