"""One-shot probe: print the shape of the all_trades_snapshot frame.

Diagnoses the single normalization error seen at engine subscribe time.
Read-only, public data, exits after ~20 seconds.
"""

import asyncio
import json

import websockets


async def grab():
    url = "wss://socket.india.delta.exchange"
    async with websockets.connect(url, open_timeout=15) as ws:
        await ws.send(json.dumps({"type": "subscribe", "payload": {"channels": [
            {"name": "all_trades", "symbols": ["BTCUSD"]}]}}))
        try:
            async with asyncio.timeout(20):
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("type") == "all_trades_snapshot":
                        return message
        except TimeoutError:
            return None


snapshot = asyncio.run(grab())
if snapshot is None:
    print("no all_trades_snapshot within 20s")
else:
    print("top-level keys:", sorted(snapshot.keys()))
    trades = snapshot.get("trades") or []
    print("trades count:", len(trades))
    if trades:
        print("first trade:", json.dumps(trades[0]))
        print("last trade :", json.dumps(trades[-1]))
