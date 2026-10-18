#!/usr/bin/env python3
"""Test đăng ký Game8.io bằng Playwright trên GitHub Actions.
Mục tiêu: xem reCAPTCHA có pass từ runner không khi submit form Sign Up
(birthDate >18 tuổi, email/phone giả)."""
import asyncio
import json
import os
import re
import sys
import time

from playwright.async_api import async_playwright

PAGE_URL = "https://game8.io/game/chess-club"
OUT_DIR = os.environ.get("G8_OUT_DIR", "/tmp/g8-test")
TS = int(time.time())
EMAIL = f"g8.test.{TS}@example.com"
PHONE = "+84912345678"
DOB_ISO = "1998-01-15"   # 28 tuổi — trên 18
DOB_DMY = "15/01/1998"

RESULT = {
    "email": EMAIL,
    "modal_opened": False,
    "recaptcha_seen": "unknown",
    "register_status": None,
    "register_body": None,
    "success_flag": None,
    "visible_error": None,
    "final_url": None,
}


async def click_if_present(page, selectors, timeout=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click(timeout=1500)
                return True
        except Exception:
            continue
    return False


async def handle_recaptcha(page) -> None:
    """Nếu widget v2 checkbox hiện thì bấm; v3 thì im lặng chờ token."""
    try:
        frames = [f for f in page.frames if "recaptcha" in (f.url or "").lower()]
        RESULT["recaptcha_seen"] = f"frames={len(frames)}"
        for f in frames:
            try:
                anchor = f.locator("#recaptcha-anchor")
                if await anchor.count():
                    await anchor.click(timeout=3000)
                    await page.wait_for_timeout(4000)
                    RESULT["recaptcha_seen"] = "clicked-checkbox"
                    return
            except Exception:
                continue
    except Exception as e:
        RESULT["recaptcha_seen"] = f"error:{e}"


async def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = await ctx.new_page()

        async def on_response(resp):
            if "auth/register" in resp.url:
                RESULT["register_status"] = resp.status
                try:
                    RESULT["register_body"] = (await resp.text())[:1000]
                except Exception:
                    pass

        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        print(f"[1] open {PAGE_URL}", flush=True)
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(5000)

        # cookie/consent nếu có
        await click_if_present(page, [
            'button:has-text("Accept")', 'button:has-text("Agree")',
            'button:has-text("I agree")', '[id*="onetrust-accept"]',
        ])

        print("[2] click Sign Up", flush=True)
        # diagnostic: liệt kê button/link nhìn thấy được
        try:
            texts = await page.locator("button, a[role='button'], a").all_inner_texts()
            uniq = []
            for t in texts:
                t = " ".join(t.split())
                if t and t not in uniq:
                    uniq.append(t)
            print("[diag] interactive texts:", uniq[:40], flush=True)
        except Exception as e:
            print(f"[diag] list failed: {e}", flush=True)
        await page.screenshot(path=f"{OUT_DIR}/step-initial.png", full_page=False)

        async def modal_ready() -> bool:
            return await page.locator('input[placeholder="First Name"]').count() > 0

        opened = False
        strategies = [
            'header button:has-text("Sign Up")',
            'button:has-text("Sign Up")',
            'a:has-text("Sign Up")',
            'button:has-text("Sign In")',
            'a:has-text("Sign In")',
            'button:has-text("Log in")',
            'a:has-text("Log in")',
            '[aria-label*="account" i]',
            '[aria-label*="user" i]',
            'button:has-text("Sign up")',
        ]
        for sel in strategies:
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    continue
                if not await loc.is_visible():
                    continue
                print(f"[2] try {sel}", flush=True)
                await loc.click(timeout=4000)
                await page.wait_for_timeout(2500)
                if await modal_ready():
                    opened = True
                    break
                # modal Login mở ra? bấm link Sign Up bên trong
                for inner in [
                    'a:has-text("Sign Up")', 'button:has-text("Sign Up")',
                    'a:has-text("Register")', 'text=Create account',
                ]:
                    try:
                        iloc = page.locator(inner).first
                        if await iloc.count() and await iloc.is_visible():
                            await iloc.click(timeout=3000)
                            await page.wait_for_timeout(2000)
                            if await modal_ready():
                                opened = True
                                break
                    except Exception:
                        continue
                if opened:
                    break
            except Exception as e:
                print(f"[2] {sel} fail: {e}", flush=True)
        RESULT["modal_opened"] = opened
        if not opened:
            print("[!] modal Sign Up không mở được", flush=True)
            await page.screenshot(path=f"{OUT_DIR}/fail-no-modal.png", full_page=True)
            with open(f"{OUT_DIR}/result.json", "w") as f:
                json.dump(RESULT, f, indent=2, ensure_ascii=False)
            print("RESULT " + json.dumps(RESULT, ensure_ascii=False), flush=True)
            await browser.close()
            return 0

        print("[3] fill form", flush=True)
        await page.fill('input[placeholder="First Name"]', "Test")
        await page.fill('input[placeholder="Last Name"]', "Player")
        await page.fill('input[placeholder="Phone Number"]', PHONE)
        dob = page.locator('input[placeholder="Date of Birth"]').first
        dob_type = await dob.get_attribute("type") or "text"
        if dob_type == "date":
            await dob.fill(DOB_ISO)
        else:
            await dob.click()
            await dob.fill(DOB_DMY)
        await page.fill('input[placeholder="Email"]', EMAIL)
        await page.fill('input[placeholder="Password"]', "Test123456!")

        print("[4] recaptcha check", flush=True)
        await handle_recaptcha(page)

        print("[5] submit Sign Up", flush=True)
        try:
            submit = page.locator('button:has-text("Sign Up")').last
            await submit.click(timeout=5000)
        except Exception as e:
            print(f"[!] submit click failed: {e}", flush=True)

        # chờ API response
        for _ in range(30):
            await page.wait_for_timeout(1000)
            if RESULT["register_status"]:
                break
            # thử lần nữa bấm submit nếu chưa có response (token recaptcha xong mới gửi)
            if RESULT["register_status"] is None and _ == 8:
                try:
                    await page.locator('button:has-text("Sign Up")').last.click(timeout=2000)
                except Exception:
                    pass

        # error text hiển thị trên modal (nếu có)
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
            for line in body_text.splitlines():
                low = line.lower()
                if any(k in low for k in (
                    "error", "invalid", "already", "failed", "16 year",
                    "18 year", "recaptcha", "captcha", "required", "not valid",
                )):
                    RESULT["visible_error"] = line.strip()[:300]
                    break
        except Exception:
            pass

        RESULT["final_url"] = page.url
        RESULT["success_flag"] = bool(
            RESULT["register_status"] == 200
            or (isinstance(RESULT.get("register_body"), str)
                and '"success":true' in RESULT["register_body"])
        )

        await page.screenshot(path=f"{OUT_DIR}/final.png", full_page=True)
        with open(f"{OUT_DIR}/result.json", "w") as f:
            json.dump(RESULT, f, indent=2, ensure_ascii=False)
        print("RESULT " + json.dumps(RESULT, ensure_ascii=False), flush=True)
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
