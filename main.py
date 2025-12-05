import os
from fastapi import FastAPI, Request, HTTPException
import httpx

app = FastAPI()

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


@app.get("/")
async def root():
    return {"status": "ok", "message": "ORB bot webhook is running"}


@app.post("/webhook")
async def tradingview_webhook(request: Request):
    """
    Minimal debug webhook:
    - Does NOT try to parse JSON
    - Just forwards the raw body text to Discord
    """
    if not DISCORD_WEBHOOK_URL:
        raise HTTPException(status_code=500, detail="DISCORD_WEBHOOK_URL not set")

    body_bytes = await request.body()
    body_text = body_bytes.decode() if body_bytes else ""

    content = (
        "**TradingView Alert Received**\n"
        "```text\n"
        f"{body_text or '[empty body]'}\n"
        "```"
    )

    async with httpx.AsyncClient() as client:
        await client.post(DISCORD_WEBHOOK_URL, json={"content": content})

    return {"status": "ok"}
