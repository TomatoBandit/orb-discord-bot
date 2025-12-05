import os
from fastapi import FastAPI, Request, HTTPException
import httpx

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

app = FastAPI()

# ---- ENV VARS ----
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_API_SECRET = os.environ.get("ALPACA_API_SECRET")


def get_trading_client() -> TradingClient:
    if not (ALPACA_API_KEY and ALPACA_API_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca env vars not set")

    # paper=True uses the Alpaca paper endpoint automatically
    return TradingClient(
        api_key=ALPACA_API_KEY,
        secret_key=ALPACA_API_SECRET,
        paper=True,
    )


# ---- BASIC HEALTH CHECK ----
@app.get("/")
async def root():
    return {"status": "ok", "message": "ORB bot webhook is running"}


# ---- ALPACA STATUS CHECK ----
@app.get("/alpaca-status")
async def alpaca_status():
    """
    Simple check: can we talk to Alpaca and get account info?
    """
    client = get_trading_client()

    try:
        account = client.get_account()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Alpaca error: {e}")

    return {
        "id": str(account.id),
        "status": str(account.status),
        "buying_power": str(account.buying_power),
        "equity": str(account.equity),
    }


# ---- TRADINGVIEW WEBHOOK ----
@app.post("/webhook")
async def tradingview_webhook(request: Request):
    """
    ORB handler:
    - Read raw body (TradingView alert message)
    - Classify as ENTRY_LONG / ENTRY_SHORT / EXIT
    - Place a simple Alpaca order (1 share QQQ) on entry
    - Close QQQ position on exit
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

    alpaca_result = "No trading action taken."

    # --- Very simple trading logic v1: 1 share QQQ ---
    try:
        client = get_trading_client()

        if event_type == "ENTRY_LONG":
            order = client.submit_order(
                order_data=MarketOrderRequest(
                    symbol=symbol,
                    qty=1,  # v1: fixed size
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            alpaca_result = f"Placed LONG market order for 1 share of {symbol}. Order ID: {order.id}"

        elif event_type == "ENTRY_SHORT":
            order = client.submit_order(
                order_data=MarketOrderRequest(
                    symbol=symbol,
                    qty=1,  # v1: fixed size
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            alpaca_result = f"Placed SHORT market order for 1 share of {symbol} (or reduced long). Order ID: {order.id}"

        elif event_type == "EXIT":
            # Close any open position in QQQ (long or short)
            try:
                close_resp = client.close_position(symbol)
                alpaca_result = f"Closed position in {symbol}. Response: {close_resp}"
            except Exception as e:
                alpaca_result = f"Tried to close position in {symbol}, but got error: {e}"

    except HTTPException:
        # Re-raise HTTP exceptions (env vars etc.)
        raise
    except Exception as e:
        alpaca_result = f"Alpaca trading error: {e}"

    # ---- Send a summary to Discord ----
    content = (
        "**ORB Signal Received**\n"
        f"Symbol: `{symbol}`\n"
        f"Event: `{event_type}`\n"
        f"Alpaca result: {alpaca_result}\n\n"
        "Raw message from TradingView:\n"
        "```text\n"
        f"{body_text or '[empty body]'}\n"
        "```"
    )

    async with httpx.AsyncClient() as client_http:
        await client_http.post(DISCORD_WEBHOOK_URL, json={"content": content})

    return {"status": "ok"}
