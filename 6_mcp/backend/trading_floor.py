from .traders import Trader
from .accounts import EXIT_CHECK_SECONDS, SHORT_TRADERS, Account
from .reset import ensure_short_traders
from typing import List
import asyncio
from .tracers import LogTracer
from agents import add_trace_processor
from .market import is_market_open
from dotenv import load_dotenv
import os

load_dotenv(override=True)

RUN_EVERY_N_MINUTES = int(os.getenv("RUN_EVERY_N_MINUTES", "40"))
RUN_EVEN_WHEN_MARKET_IS_CLOSED = (
    os.getenv("RUN_EVEN_WHEN_MARKET_IS_CLOSED", "false").strip().lower() == "true"
)
USE_MANY_MODELS = os.getenv("USE_MANY_MODELS", "false").strip().lower() == "true"

names = ["Warren", "George", "Ray", "Cathie", "Santy", "Cathe"]
lastnames = ["Patience", "Bold", "Systematic", "Crypto", "Scalp", "Sniper"]

if USE_MANY_MODELS:
    model_names = [
        "gpt-5.5",
        "deepseek-v4-flash",
        "gemini-3.5-flash",
        "grok-4.3",
        "deepseek-v4-flash",
        "deepseek-v4-flash",
    ]
    short_model_names = [
        "GPT 5.5",
        "DeepSeek V4",
        "Gemini 3.5 Flash",
        "Grok 4.3",
        "DeepSeek V4 Flash",
        "DeepSeek V4 Flash",
    ]
else:
    model_names = ["deepseek-v4-flash"] * 6
    short_model_names = ["DeepSeek V4 Flash"] * 6


def create_traders() -> List[Trader]:
    traders = []
    for name, lastname, model_name in zip(names, lastnames, model_names):
        traders.append(Trader(name, lastname, model_name))
    return traders


async def watch_exits():
    """Close a position when its stop hits, without waiting for the model."""
    while True:
        for name in names:
            try:
                for note in Account.get(name).enforce_exits():
                    print(note)
            except Exception as error:
                print(f"Exit check failed for {name}: {error}")
        await asyncio.sleep(EXIT_CHECK_SECONDS)


def traders_due(traders: List[Trader]) -> List[Trader]:
    """Santy and Cathe run even after the close. The other four wait for the open."""
    if RUN_EVEN_WHEN_MARKET_IS_CLOSED or is_market_open():
        return traders
    return [trader for trader in traders if trader.name.lower() in SHORT_TRADERS]


async def run_every_n_minutes():
    add_trace_processor(LogTracer())
    ensure_short_traders()
    traders = create_traders()
    asyncio.create_task(watch_exits())
    while True:
        due = traders_due(traders)
        if len(due) < len(traders):
            print("Market is closed, running Santy and Cathe only")
        await asyncio.gather(*[trader.run() for trader in due])
        await asyncio.sleep(RUN_EVERY_N_MINUTES * 60)


async def run_short_desk():
    """Keep Santy and Cathe trading on their own loop."""
    add_trace_processor(LogTracer())
    ensure_short_traders()
    traders = [trader for trader in create_traders() if trader.name.lower() in SHORT_TRADERS]
    print("Starting Santy and Cathe")
    asyncio.create_task(watch_exits())
    while True:
        await asyncio.gather(*[trader.run() for trader in traders])
        await asyncio.sleep(RUN_EVERY_N_MINUTES * 60)


if __name__ == "__main__":
    print(f"Starting scheduler to run every {RUN_EVERY_N_MINUTES} minutes")
    asyncio.run(run_every_n_minutes())
