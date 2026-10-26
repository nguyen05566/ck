#!/usr/bin/env python3
"""
ws_probe.py — Bắt WebSocket của game để phân tích protocol (mẫu arena10k.py).

Cách dùng:
    python3 ws_probe.py --cookie ck1.txt --game tienlen_miennam --seconds 60
    python3 ws_probe.py --cookie ck1.txt --game tienlen_miennam --claim 3 --transfer 68952059

Kết quả lưu vào ws_out/:
    ws_frames.json  - toàn bộ frame gửi/nhận (nhị phân -> base64) + tên lệnh
    ws_frames.bin   - ghép các frame nhị phân theo thứ tự để đọc hex
"""
import argparse
import http.cookies
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ws_bridge  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

GAMES = {
    "tienlen_miennam": "https://www.facebook.com/gaming/play/tienlen_miennam",
    "phom_tala": "https://www.facebook.com/gaming/play/phom_tala",
    "gomoku-vh": "https://www.facebook.com/gaming/play/gomoku-vh",
    "chinese-chess-vh": "https://www.facebook.com/gaming/play/chinese-chess-vh",
    "mystery-xiangqi": "https://www.facebook.com/gaming/play/mystery-xiangqi",
    "maubinh_xapxam": "https://www.facebook.com/gaming/play/maubinh_xapxam",
    "sam_loc_vh": "https://www.facebook.com/gaming/play/sam_loc_vh",
}

JS_CLAIM = """(timeoutMs) => new Promise((resolve) => {
    try {
        if (!window.connection || !connection.ws || connection.ws.readyState !== 1) {
            resolve({success:false, error:'ws not connected'}); return;
        }
        const msg = new OutboundMessage("VIDEO_REWARD");
        msg.writeByte(1);
        let resolved = false;
        connection.send(msg, function(response, success) {
            if (resolved) return; resolved = true;
            if (success) {
                try { resolve({success:true, amount: response.readLong()}); }
                catch(e) { resolve({success:true, amount:0}); }
            } else { resolve({success:false, error:'no response'}); }
        });
        setTimeout(() => { if (!resolved) { resolved = true;
            resolve({success:false, error:'timeout'}); } }, timeoutMs);
    } catch(e) { resolve({success:false, error:e.toString()}); }
})"""

JS_TRANSFER = """(destId) => new Promise((resolve) => {
    try {
        const balEl = document.querySelector('.chipBalance');
        const balText = balEl ? balEl.textContent.trim() : '0';
        let balance = 0;
        const cleaned = balText.replace(/[^0-9kK.]/g, '');
        if (cleaned.toLowerCase().endsWith('k')) balance = Math.round(parseFloat(cleaned.slice(0, -1)) * 1000);
        else if (cleaned) balance = parseInt(cleaned) || 0;
        if (!window.connection || !connection.ws || connection.ws.readyState !== 1) {
            resolve({success:false, error:'ws not connected', balance:balance}); return;
        }
        const msg = new OutboundMessage("TRANSFER");
        msg.writeLong(destId);
        msg.writeLong(balance);
        let resolved = false;
        connection.send(msg, function(resp, ok) {
            if (resolved) return; resolved = true;
            try { resolve({success:ok, status: resp.readSignedByte(), balance:balance}); }
            catch(e) { resolve({success:ok, error:e.toString(), balance:balance}); }
        });
        setTimeout(() => { if (!resolved) { resolved = true;
            resolve({success:false, error:'timeout', balance:balance}); } }, 12000);
    } catch(e) { resolve({success:false, error:e.toString()}); }
})"""


def log(msg):
    print(f"[probe] {msg}", flush=True)


def load_cookie(path):
    content = open(path, "r", encoding="utf-8").read().strip().strip('"').strip("'")
    content = " ".join(content.split()).replace(";  ", "; ").replace(" ;", ";")
    parsed = http.cookies.SimpleCookie()
    try:
        parsed.load(content)
    except Exception:
        parsed = None
    out = []
    if parsed:
        out = [{"name": n, "value": mv.value, "domain": ".facebook.com", "path": "/",
                "secure": True, "httpOnly": False, "sameSite": "Lax"}
               for n, mv in parsed.items() if n and mv.value]
    if not out:
        for part in content.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            n, v = part.split("=", 1)
            if n.strip() and v.strip():
                out.append({"name": n.strip(), "value": v.strip(), "domain": ".facebook.com",
                            "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax"})
    return out


def find_game_frame(page):
    deadline = time.time() + 60
    while time.time() < deadline:
        for f in page.frames:
            try:
                if f.is_detached():
                    continue
                ok = f.evaluate("() => !!(window.connection || window.OutboundMessage)")
                if ok:
                    return f
            except Exception:
                continue
        time.sleep(2)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie", required=True, help="file cookie .txt")
    ap.add_argument("--game", default="tienlen_miennam", choices=sorted(GAMES))
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--claim", type=int, default=0, help="số lần claim để bắt frame VIDEO_REWARD")
    ap.add_argument("--transfer", default="", help="dest id để bắt frame TRANSFER")
    ap.add_argument("--out", default="ws_out")
    args = ap.parse_args()

    cookies = load_cookie(args.cookie)
    if not cookies:
        log("Không parse được cookie")
        return 1
    log(f"cookie: {len(cookies)} mục từ {args.cookie}")

    with sync_playwright() as p:
        context, profile_dir = ws_bridge.launch_private_context(
            p, headless=True, context_kwargs={"viewport": {"width": 1440, "height": 900}, "locale": "vi-VN"})
        try:
            priv = context.new_page()
            ws_bridge.wipe_storage(context, priv)
            priv.close()
            ws_bridge.install_ws_hook(context)
            context.add_cookies(cookies)

            page = context.new_page()
            page.set_default_timeout(60000)
            log(f"mở FB -> {GAMES[args.game]}")
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
            log("mở game...")
            page.goto(GAMES[args.game], wait_until="domcontentloaded", timeout=90000)
            time.sleep(20000)

            gf = find_game_frame(page)
            if gf is None:
                log("KHÔNG tìm thấy game frame")
            else:
                log(f"game frame: {gf.url[:90]}")

            log(f"bắt frame trong {args.seconds}s...")
            end = time.time() + args.seconds
            claims = 0
            while time.time() < end:
                if gf is not None and claims < args.claim:
                    try:
                        res = gf.evaluate(JS_CLAIM, 12000)
                        log(f"  claim -> {res}")
                        claims += 1
                    except Exception as e:
                        log(f"  claim lỗi: {e}")
                    time.sleep(3)
                    continue
                time.sleep(2)

            if args.transfer and gf is not None:
                try:
                    res = gf.evaluate(JS_TRANSFER, int(args.transfer))
                    log(f"  transfer -> {res}")
                except Exception as e:
                    log(f"  transfer lỗi: {e}")
                time.sleep(3)

            frames, cmds = ws_bridge.capture_all(page)
            log(f"tổng frame: {len(frames)} (out={sum(1 for f in frames if f['dir'] == 'out')}), lệnh: {len(cmds)}")
            for c in cmds[:20]:
                log(f"  cmd {c.get('name')} keys={c.get('keys')}")
            json_path, bin_path = ws_bridge.dump_frames(frames, args.out)
            log(f"lưu: {json_path} + {bin_path}")
            try:
                ws_bridge.wipe_storage(context, page)
            except Exception:
                pass
        finally:
            ws_bridge.close_private_context(context, profile_dir)
            log("đã đóng browser + xoá private profile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
