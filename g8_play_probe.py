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

AD_HOSTS = (
    "googlesyndication", "doubleclick", "googleads", "gstatic",
    "google-analytics", "googleapis", "cloudflareinsights", "googlevideo",
    "ytimg", "adtrafficquality", "googleadservices", "fundingchoices",
    "googletagmanager", "google.com", "googleusercontent",
)


def record(rtype, url, method="", status=None):
    if any(h in url for h in AD_HOSTS):
        return
    key = (rtype, method, url)
    if key in SEEN:
        return
    SEEN.add(key)
    host = re.sub(r"^(https?|wss?)://([^/]+).*$", r"\2", url)
    RESULT["hosts"][host] = RESULT["hosts"].get(host, 0) + 1
    NET.append({"type": rtype, "method": method, "status": status, "url": url[:400]})


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

        # pre-roll / fullscreen ad — Close nằm TRONG iframe chéo-origin của ad
        for i in range(22):
            await page.wait_for_timeout(2000)
            closed = False
            for fr in page.frames:
                try:
                    loc = fr.get_by_text("Close", exact=True).first
                    if await loc.count():
                        await loc.click(timeout=1200)
                        RESULT["notes"].append(f"closed_ad@{i*2}s frame={fr.url[:60]}")
                        closed = True
                        break
                except Exception:
                    continue
            if closed:
                break

        await page.wait_for_timeout(15000)

        # iframe trong #gameContainer
        try:
            srcs = await page.eval_on_selector_all(
                "#gameContainer iframe",
                "els => els.map(e => e.src)",
            )
            RESULT["game_iframe"] = srcs
            RESULT["notes"].append(f"gameContainer_iframes={len(srcs)}")
        except Exception as e:
            RESULT["notes"].append(f"iframe_err:{e}")

        frames = [f.url[:200] for f in page.frames]
        RESULT["frames"] = frames
        RESULT["notes"].append(f"main={page.url[:120]}")

        RESULT["requests"] = NET
        with open(f"{OUT_DIR}/play-probe.json", "w") as fh:
            json.dump(RESULT, fh, indent=2, ensure_ascii=False)
        print("HOSTS " + json.dumps(RESULT["hosts"], ensure_ascii=False), flush=True)
        print("NOTES " + json.dumps(RESULT["notes"], ensure_ascii=False), flush=True)
        print("GAME_IFRAME " + json.dumps(RESULT.get("game_iframe"), ensure_ascii=False), flush=True)
        for r in NET:
            u = r["url"]
            if any(k in u for k in ("/api/", "e6cf", "ws", "index.html", "session", "auth")):
                print("REQ " + json.dumps(r, ensure_ascii=False), flush=True)
        await page.screenshot(path=f"{OUT_DIR}/play-final.png", full_page=False)
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
