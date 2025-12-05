import os
import json
from fastapi import FastAPI, Request, HTTPException
import httpx

app = FastAPI()

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
WEBHOOK_SECRET = os.environ.get("TRADINGVIEW_WEBHOOK_SECRET")  # optional, but recommended


@app.get("/")
async def root():
    return {"status": "ok", "message": "ORB bot webhook is running"}


@app.post("/webhook")
async def tradingview_webhook(request: Request):
    if not DISCORD_WEBHOOK_URL:
        raise HTTPException(status_code=500, detail="DISCORD_WEBHOOK_URL not set")

    data = await request.json()

    # Optional: secret check for basic security
    if WEBHOOK_SECRET:
        incoming_secret = data.get("secret")
        if incoming_secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # Nicely format the payload for Discord so we can debug
    pretty = json.dumps(data, indent=2)

    message = (
        "**TradingView Alert Received**\n"
        "```json\n"
        f"{pretty}\n"
        "```"
    )

    async with httpx.AsyncClient() as client:
        await client.post(DISCORD_WEBHOOK_URL, json={"content": message})

    return {"status": "ok"}
