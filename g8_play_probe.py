#!/usr/bin/env python3
"""Probe: guest player bấm Play trên Chess club — ghi lại toàn bộ network
(HTTP + WS) để tìm API tạo bàn / vào bàn / matchmaking."""
import asyncio
import json
import os
import re
import time

from playwright.async_api import async_playwright

PAGE_URL = "https://game8.io/game/chess-club"
OUT_DIR = os.environ.get("G8_OUT_DIR", "/tmp/g8-test")

NET = []          # {url, method, status, type}
SEEN = set()
RESULT = {"requests": [], "hosts": {}, "game_iframe": None, "notes": []}


def record(rtype, url, method="", status=None):
    key = (rtype, method, url)
    if key in SEEN:
        return
    SEEN.add(key)
    host = re.sub(r"^(https?|wss?)://([^/]+).*$", r"\2", url)
    RESULT["hosts"][host] = RESULT["hosts"].get(host, 0) + 1
    NET.append({"type": rtype, "method": method, "status": status, "url": url[:300]})


async def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        page = await ctx.new_page()

        page.on("request", lambda r: record(
            "ws" if r.resource_type == "websocket" else "req",
            r.url, r.method))
        page.on("response", lambda r: record(
            "resp", r.url, r.request.method, r.status))

        print("[1] open page", flush=True)
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(6000)

        # iframe game (nếu plugin load được)
        for f in page.frames:
            if f != page.main_frame:
                RESULT["game_iframe"] = f.url[:200]

        print("[2] click Play now", flush=True)
        for sel in ['button:has-text("Play now")', 'text=Play now']:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=4000)
                    break
            except Exception as e:
                print(f"[!] {sel}: {e}", flush=True)

        await page.wait_for_timeout(15000)

        # iframe sau khi play
        frames = []
        for f in page.frames:
            frames.append(f.url[:200])
        RESULT["frames"] = frames
        RESULT["notes"].append(f"main={page.url[:120]}")

        # text trong game container
        try:
            gc = page.locator("#gameContainer, .game-frame, iframe")
            RESULT["notes"].append("game_el=" + str(await gc.count()))
        except Exception:
            pass

        RESULT["requests"] = NET[:200]
        with open(f"{OUT_DIR}/play-probe.json", "w") as fh:
            json.dump(RESULT, fh, indent=2, ensure_ascii=False)
        print("HOSTS " + json.dumps(RESULT["hosts"], ensure_ascii=False), flush=True)
        interesting = [
            r for r in NET
            if any(k in r["url"].lower() for k in (
                "ws", "room", "match", "table", "lobby", "game", "player",
                "session", "socket", "channel", "queue",
            ))
        ]
        print("INTERESTING " + json.dumps(interesting[:40], ensure_ascii=False), flush=True)
        await page.screenshot(path=f"{OUT_DIR}/play-final.png", full_page=False)
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
