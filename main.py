import os
from fastapi import FastAPI, Request, HTTPException
import httpx

app = FastAPI()

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAccountRequest
from alpaca.trading.enums import AccountStatus

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_API_SECRET = os.environ.get("ALPACA_API_SECRET")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL")


@app.get("/alpaca-status")
async def alpaca_status():
    """
    Simple check: can we talk to Alpaca and get account info?
    """
    if not (ALPACA_API_KEY and ALPACA_API_SECRET and ALPACA_BASE_URL):
        raise HTTPException(status_code=500, detail="Alpaca env vars not set")

    try:
        client = TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_API_SECRET,
            paper=True,
            url=ALPACA_BASE_URL,
        )
        account = client.get_account()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Alpaca error: {e}")

    return {
        "id": str(account.id),
        "status": str(account.status),
        "buying_power": str(account.buying_power),
        "equity": str(account.equity),
    }


@app.get("/")
async def root():
    return {"status": "ok", "message": "ORB bot webhook is running"}


@app.post("/webhook")
async def tradingview_webhook(request: Request):
    """
    Minimal ORB handler:
    - Read raw body (TradingView alert message)
    - Classify as ENTRY_LONG / ENTRY_SHORT / EXIT
    - Send a clear message to Discord
    """
    if not DISCORD_WEBHOOK_URL:
        raise HTTPException(status_code=500, detail="DISCORD_WEBHOOK_URL not set")

    body_bytes = await request.body()
    body_text = body_bytes.decode() if body_bytes else ""

    # Default values
    event_type = "UNKNOWN"
    symbol = "QQQ"  # v1 is QQQ only

    if "ORB_QQQ_ENTRY_LONG" in body_text:
        event_type = "ENTRY_LONG"
    elif "ORB_QQQ_ENTRY_SHORT" in body_text:
        event_type = "ENTRY_SHORT"
    elif "ORB_QQQ_EXIT" in body_text:
        event_type = "EXIT"

    content = (
        "**ORB Signal Received**\n"
        f"Symbol: `{symbol}`\n"
        f"Event: `{event_type}`\n"
        "Raw message:\n"
        "```text\n"
        f"{body_text or '[empty body]'}\n"
        "```"
    )

    async with httpx.AsyncClient() as client:
        await client.post(DISCORD_WEBHOOK_URL, json={"content": content})

    return {"status": "ok"}
