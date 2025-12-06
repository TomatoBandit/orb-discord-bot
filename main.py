import os
import json
from datetime import datetime, timezone

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

RISK_PER_TRADE = 0.005         # 0.5% of equity per trade
MAX_DAILY_LOSS_R = 2.0         # -2R per day
MAX_TRADES_PER_DAY = 3         # 3 entries per day (combined across symbols)


def get_trading_client() -> TradingClient:
    if not (ALPACA_API_KEY and ALPACA_API_SECRET):
        raise HTTPException(status_code=500, detail="Alpaca env vars not set")

    return TradingClient(
        api_key=ALPACA_API_KEY,
        secret_key=ALPACA_API_SECRET,
        paper=True,  # paper endpoint
    )


def symbol_for_positions(symbol: str) -> str:
    """
    Alpaca quirk:
    - Orders: 'BTC/USD' works
    - Positions endpoints (get_open_position, close_position): often expect 'BTCUSD'
    For non-crypto symbols like 'QQQ', this just returns the symbol unchanged.
    """
    return symbol.replace("/", "") if "/" in symbol else symbol


def tif_for_symbol(symbol: str) -> TimeInForce:
    """
    Alpaca crypto does NOT allow TimeInForce.DAY.
    - For crypto symbols (with '/'), use GTC.
    - For regular stock/ETF symbols, use DAY.
    """
    if "/" in symbol:
        return TimeInForce.GTC
    return TimeInForce.DAY


# ---- SIMPLE IN-MEMORY DAILY STATE ----
current_day = None
daily_loss_r = 0.0
trade_count = 0


def reset_daily_if_needed():
    """Reset daily counters if date changed (UTC-based, good enough for testing)."""
    global current_day, daily_loss_r, trade_count
    today = datetime.now(timezone.utc).date()
    if current_day != today:
        current_day = today
        daily_loss_r = 0.0
        trade_count = 0


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
    ORB / BTC handler with:
    - Risk-based entries (0.5% per trade, fractional size)
    - 50% partial at 2R
    - Final exit (trailing stop or EOD)
    - Daily guardrails: max 3 trades, max -2R per day (combined across symbols)
    """
    if not DISCORD_WEBHOOK_URL:
        raise HTTPException(status_code=500, detail="DISCORD_WEBHOOK_URL not set")

    body_bytes = await request.body()
    body_text = body_bytes.decode() if body_bytes else ""

    reset_daily_if_needed()

    data = None
    event_type = "UNKNOWN"
    symbol = "QQQ"
    reason = None
    alpaca_result = "No trading action taken."
    guardrail_info = ""

    # Try to parse JSON from TradingView
    if body_text.strip().startswith("{"):
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError:
            data = None

    if data:
        event_type = data.get("event", "UNKNOWN")
        symbol = data.get("symbol", "QQQ")
        reason = data.get("reason")
    else:
        # Fallback for any legacy plain-text alerts
        if "ORB_QQQ_ENTRY_LONG" in body_text:
            event_type = "ENTRY_LONG"
            symbol = "QQQ"
        elif "ORB_QQQ_ENTRY_SHORT" in body_text:
            event_type = "ENTRY_SHORT"
            symbol = "QQQ"
        elif "ORB_QQQ_EXIT" in body_text:
            event_type = "FINAL_EXIT"
            symbol = "QQQ"

    global daily_loss_r, trade_count

    try:
        client = get_trading_client()

        # ---- ENTRIES: 0.5% risk, fractional size, daily guardrails ----
        if event_type in ("ENTRY_LONG", "ENTRY_SHORT") and data:
            # Guardrails: check before placing order
            if daily_loss_r <= -MAX_DAILY_LOSS_R:
                alpaca_result = (
                    f"Entry blocked: daily loss limit reached (daily_loss_r={daily_loss_r}R)."
                )
                guardrail_info = "Blocked by -2R daily limit."
            elif trade_count >= MAX_TRADES_PER_DAY:
                alpaca_result = (
                    f"Entry blocked: max trades per day reached (trade_count={trade_count})."
                )
                guardrail_info = "Blocked by 3-trades-per-day limit."
            else:
                # Proceed with risk-based sizing
                try:
                    entry_price = float(data.get("entryPrice"))
                    or_high = float(data.get("orHigh"))
                    or_low = float(data.get("orLow"))
                except (TypeError, ValueError):
                    entry_price = None

                if entry_price is None or entry_price <= 0:
                    alpaca_result = "Missing or invalid price data for risk sizing."
                else:
                    # Determine stop based on OR or ATR-encoded stop
                    if event_type == "ENTRY_LONG":
                        stop_price = or_low
                    else:  # ENTRY_SHORT
                        stop_price = or_high

                    risk_per_unit = abs(entry_price - stop_price)

                    if risk_per_unit <= 0:
                        alpaca_result = (
                            f"Invalid risk_per_unit ({risk_per_unit}), no order placed."
                        )
                    else:
                        account = client.get_account()
                        equity = float(str(account.equity))

                        if equity <= 0:
                            alpaca_result = (
                                f"Equity <= 0 (equity={equity}), no order placed."
                            )
                        else:
                            # 1) Size from risk (0.5% R)
                            dollar_risk = equity * RISK_PER_TRADE
                            qty_from_risk = dollar_risk / risk_per_unit

                            # 2) Cap by what the account can actually afford notionally
                            max_qty_notional = equity / entry_price
                            qty = min(qty_from_risk, max_qty_notional)

                            if qty <= 0:
                                alpaca_result = (
                                    f"Calculated qty <= 0 (qty={qty}), no order placed."
                                )
                            else:
                                side = (
                                    OrderSide.BUY
                                    if event_type == "ENTRY_LONG"
                                    else OrderSide.SELL
                                )

                                tif = tif_for_symbol(symbol)

                                # Use 'symbol' exactly as sent from TradingView for orders
                                order = client.submit_order(
                                    order_data=MarketOrderRequest(
                                        symbol=symbol,
                                        qty=qty,  # fractional qty
                                        side=side,
                                        time_in_force=tif,
                                    )
                                )
                                trade_count += 1
                                alpaca_result = (
                                    f"Placed {event_type} market order for ~{qty:.6f} units of {symbol}. "
                                    f"Order ID: {order.id}. "
                                    f"trade_count today = {trade_count}."
                                )

        # ---- PARTIAL EXIT: close 50% of current position ----
        elif event_type == "PARTIAL_EXIT":
            try:
                # Use normalized symbol for positions API (e.g. BTCUSD)
                pos_symbol = symbol_for_positions(symbol)
                position = client.get_open_position(pos_symbol)
                pos_qty = float(str(position.qty))
                pos_side = str(position.side).lower()  # 'long' or 'short'

                if pos_qty <= 0:
                    alpaca_result = "No open position size to partially exit."
                else:
                    half_qty = pos_qty / 2.0
                    exit_side = (
                        OrderSide.SELL if pos_side == "long" else OrderSide.BUY
                    )

                    tif = tif_for_symbol(symbol)

                    # Use original 'symbol' for the order itself
                    order = client.submit_order(
                        order_data=MarketOrderRequest(
                            symbol=symbol,
                            qty=half_qty,
                            side=exit_side,
                            time_in_force=tif,
                        )
                    )
                    alpaca_result = (
                        f"PARTIAL_EXIT: Closed ~50% ({half_qty:.6f} units) of {symbol} "
                        f"({pos_side}). Order ID: {order.id}"
                    )
            except Exception as e:
                alpaca_result = f"Error during PARTIAL_EXIT for {symbol}: {e}"

        # ---- FINAL EXIT: close entire position & update daily R if stop loser ----
        elif event_type == "FINAL_EXIT":
            try:
                close_symbol = symbol_for_positions(symbol)
                close_resp = client.close_position(close_symbol)
                alpaca_result = (
                    f"FINAL_EXIT: Closed position in {symbol} "
                    f"(pos symbol {close_symbol}). Response: {close_resp}"
                )

                # If this was a full stop before any partial was taken, count -1R
                if reason == "STOP_PHASE1":
                    daily_loss_r -= 1.0
                    guardrail_info = (
                        f"Recorded -1R loss (reason=STOP_PHASE1). "
                        f"daily_loss_r now = {daily_loss_r}R."
                    )

            except Exception as e:
                alpaca_result = (
                    f"Tried to close position in {symbol} "
                    f"(pos symbol {symbol_for_positions(symbol)}), but got error: {e}"
                )

    except HTTPException:
        raise
    except Exception as e:
        alpaca_result = f"Alpaca trading error: {e}"

    # ---- Discord summary ----
    content = (
        "**ORB Signal Received**\n"
        f"Symbol: `{symbol}`\n"
        f"Event: `{event_type}`\n"
        f"Reason: `{reason}`\n"
        f"Alpaca result: {alpaca_result}\n"
        f"Daily loss (R): {daily_loss_r}\n"
        f"Trades today: {trade_count}\n"
        f"Guardrail info: {guardrail_info or 'None'}\n\n"
        "Raw message from TradingView:\n"
        "```text\n"
        f"{body_text or '[empty body]'}\n"
        "```"
    )

    async with httpx.AsyncClient() as client_http:
        await client_http.post(DISCORD_WEBHOOK_URL, json={"content": content})

    return {"status": "ok"}
