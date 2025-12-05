import os
import json
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

RISK_PER_TRADE = 0.005  # 0.5%


def get_trading_client() -> TradingClient:
    if not (ALPACA_API_KEY and ALPACA_API_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca env vars not set")

    # paper=True uses Alpaca paper endpoint
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
    ORB handler with risk-based sizing:
    - Parse JSON from TradingView (event, symbol, orHigh/orLow, entryPrice)
    - Compute qty so that loss at stop = 0.5% of equity
    - ENTRY_LONG / ENTRY_SHORT -> market order with fractional qty
    - EXIT -> close QQQ position
    - Always send a summary to Discord
    """
    if not DISCORD_WEBHOOK_URL:
        raise HTTPException(status_code=500, detail="DISCORD_WEBHOOK_URL not set")

    body_bytes = await request.body()
    body_text = body_bytes.decode() if body_bytes else ""

    data = None
    event_type = "UNKNOWN"
    symbol = "QQQ"
    alpaca_result = "No trading action taken."

    # Try to parse JSON payload from TradingView
    if body_text.strip().startswith("{"):
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError:
            data = None

    if data:
        event_type = data.get("event", "UNKNOWN")
        symbol = data.get("symbol", "QQQ")
    else:
        # Fallback if we ever get plain text again
        if "ORB_QQQ_ENTRY_LONG" in body_text:
            event_type = "ENTRY_LONG"
        elif "ORB_QQQ_ENTRY_SHORT" in body_text:
            event_type = "ENTRY_SHORT"
        elif "ORB_QQQ_EXIT" in body_text:
            event_type = "EXIT"

    # ---- TRADING LOGIC v1: RISK-BASED SIZING, SIMPLE EXITS ----
    try:
        client = get_trading_client()

        if event_type in ("ENTRY_LONG", "ENTRY_SHORT") and data:
            try:
                entry_price = float(data.get("entryPrice"))
                or_high = float(data.get("orHigh"))
                or_low = float(data.get("orLow"))
            except (TypeError, ValueError):
                entry_price = None

            if entry_price is None:
                alpaca_result = "Missing or invalid price data for risk sizing."
            else:
                # Determine stop price based on OR
                if event_type == "ENTRY_LONG":
                    stop_price = or_low
                else:  # ENTRY_SHORT
                    stop_price = or_high

                risk_per_share = abs(entry_price - stop_price)

                if risk_per_share <= 0:
                    alpaca_result = f"Invalid risk_per_share ({risk_per_share}), no order placed."
                else:
                    # Get account equity for 0.5% risk
                    account = client.get_account()
                    equity = float(str(account.equity))
                    dollar_risk = equity * RISK_PER_TRADE

                    qty = dollar_risk / risk_per_share

                    if qty <= 0:
                        alpaca_result = f"Calculated qty <= 0 (qty={qty}), no order placed."
                    else:
                        side = OrderSide.BUY if event_type == "ENTRY_LONG" else OrderSide.SELL

                        order = client.submit_order(
                            order_data=MarketOrderRequest(
                                symbol=symbol,
                                qty=qty,  # fractional qty allowed in paper
                                side=side,
                                time_in_force=TimeInForce.DAY,
                            )
                        )
                        alpaca_result = (
                            f"Placed {event_type} market order for ~{qty:.4f} shares of {symbol}. "
                            f"Order ID: {order.id}"
                        )

        elif event_type == "EXIT":
            try:
                close_resp = client.close_position(symbol)
                alpaca_result = f"Closed position in {symbol}. Response: {close_resp}"
            except Exception as e:
                alpaca_result = f"Tried to close position in {symbol}, but got error: {e}"

    except HTTPException:
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
