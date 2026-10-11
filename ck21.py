#!/usr/bin/env python3
"""FB Tien Len Mien Nam reward bot v9 — BATCH TRANSFER MODE
★ Claim 40 lần rồi transfer, lặp lại cho đến hết MAX_RUNTIME
Login FB và load game 1 LẦN duy nhất ở đầu session, sau đó:
  loop: claim 40 lần → transfer → claim 40 lần → transfer → ... cho tới hết MAX_RUNTIME"""

import os, sys, time, re, random

# Thử import module bổ trợ nếu có (không bắt buộc)
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import board_dom_merged as m
except Exception:
    m = None

# === Module chống khóa nick (anti-ban) ===
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import anti_lock as al
except Exception as e:
    print(f"[WARN] Không nạp được anti_lock.py — chạy không có chống khóa nick: {e}", flush=True)
    al = None

from playwright.sync_api import sync_playwright

GAME_URL = "https://www.facebook.com/gaming/play/maubinh_xapxam"
CLAIM_BATCH = int(os.environ.get("CLAIM_BATCH", "40"))  # Số claim trước khi transfer
MAX_CYCLES = CLAIM_BATCH
DELAY = float(os.environ.get("COOLDOWN", "3"))
REST = int(os.environ.get("REST_BETWEEN_RUNS", "3"))
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", str(330 * 60)))
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"

# ============ TRANSFER LOGIC ============
# Transfer xu về hub account sau mỗi CLAIM_BATCH lần claim
TRANSFER_DEST_ID = int(os.environ.get("TRANSFER_DEST_ID", "51977054"))
TRANSFER_ENABLED = os.environ.get("TRANSFER_ENABLED", "true").lower() == "true"

# ============ SINGLE COOKIE MODE ============
SINGLE_COOKIE_FILE = os.environ.get("SINGLE_COOKIE_FILE", "ck15.txt").strip()

# ============ Pre-claim transfer threshold ============
# Nếu balance > ngưỡng này (xu), transfer trước khi claim tiếp
PRE_CLAIM_TRANSFER_THRESHOLD = int(os.environ.get("PRE_CLAIM_TRANSFER_THRESHOLD", "10000"))

# ---- Quota hàng ngày (chống spam server) ----
MAX_CLAIMS_PER_DAY         = int(os.environ.get("MAX_CLAIMS_PER_DAY", str(getattr(al, "DEFAULT_MAX_CLAIMS_PER_DAY", 180) if al else 180)))
MAX_TRANSFERS_PER_DAY      = int(os.environ.get("MAX_TRANSFERS_PER_DAY", str(getattr(al, "DEFAULT_MAX_TRANSFERS_PER_DAY", 30) if al else 30)))
MAX_TRANSFER_AMOUNT_PER_DAY = int(os.environ.get("MAX_TRANSFER_AMOUNT_PER_DAY", str(getattr(al, "DEFAULT_MAX_TRANSFER_AMOUNT", 2_000_000) if al else 2_000_000)))

# ---- Random session start offset (giây) ----
SESSION_START_JITTER = int(os.environ.get("SESSION_START_JITTER", "180"))

# ---- Idle break mỗi N claim ----
IDLE_EVERY_CLAIMS = int(os.environ.get("IDLE_EVERY_CLAIMS", "60"))

# ---- Có bật warm-up account trước khi vào game không? ----
WARMUP_ENABLED = os.environ.get("WARMUP_ENABLED", "true").lower() == "true"


def parse_balance_num(bal_text):
    """Parse '56.4k' or '123,456' or '78900' → int."""
    if not bal_text or bal_text == '?':
        return 0
    s = str(bal_text).strip().lower().replace(',', '').replace(' ', '')
    try:
        if s.endswith('k'):
            return int(float(s[:-1]) * 1000)
        if s.endswith('m'):
            return int(float(s[:-1]) * 1000000)
        return int(float(s))
    except Exception:
        return 0


# ============================================================
# ĐỌC COOKIE TỪ FILE DUY NHẤT
# ============================================================
def load_single_cookie_set(path):
    """Đọc đúng 1 file cookie. Trả về [{"file": "...", "raw": "..."}] hoặc []."""
    if not os.path.exists(path):
        print(f"[COOKIE] ❌ Không tìm thấy file: {path}", flush=True)
        return []

    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read().strip()
    except Exception as e:
        print(f"[COOKIE] ❌ Lỗi đọc {path}: {e}", flush=True)
        return []

    if not content:
        print(f"[COOKIE] ❌ {path} rỗng", flush=True)
        return []

    # Chuẩn hoá: bỏ nháy, gộp xuống dòng/tab, chuẩn hoá dấu ;
    content = content.strip('"').strip("'")
    content = " ".join(content.split())
    content = content.replace(";  ", "; ").replace(" ;", ";")

    print(f"[COOKIE] ✅ Nạp {os.path.basename(path)} ({len(content)} ký tự)", flush=True)
    return [{"file": os.path.basename(path), "raw": content}]


def parse_cookie(raw: str):
    """Parse chuỗi cookie header thành list dict cho Playwright."""
    raw = raw.strip().strip('"').strip("'")
    raw = " ".join(raw.split())
    raw = raw.replace(";  ", "; ").replace(" ;", ";")

    # Ưu tiên dùng module bổ trợ nếu có
    if m is not None and hasattr(m, "parse_cookie_header"):
        try:
            return m.parse_cookie_header(raw)
        except Exception:
            pass

    # Fallback: http.cookies chuẩn
    import http.cookies
    parsed = http.cookies.SimpleCookie()
    parsed.load(raw)
    return [
        {"name": n, "value": mv.value, "domain": ".facebook.com",
         "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax"}
        for n, mv in parsed.items() if n and mv.value
    ]


# ============================================================
# HELPER
# ============================================================
def get_bal(gf):
    try:
        return gf.evaluate(
            "() => document.querySelector('.chipBalance')?.textContent.trim() || '?'"
        )
    except Exception:
        return "?"


def transfer_all_xu(gf, page, dest_id=TRANSFER_DEST_ID):
    """Transfer ALL current xu về dest_id via game's connection.send.

    If WS not connected, reload page (via ensure_ws_connected) and retry.
    Returns dict {success, amount, status, message}.
    """
    # Ensure WS is connected (reload page if needed)
    if not ensure_ws_connected(gf, page):
        return {"success": False, "error": "ws reconnect failed after reloads"}

    # After reload, re-find game frame (gf may have changed)
    gf_new = find_gf(page, max_wait=30)
    if gf_new:
        gf = gf_new

    try:
        result = gf.evaluate("""(destId) => {
            return new Promise((resolve) => {
                try {
                    // 1. Read balance from .chipBalance DOM
                    const balEl = document.querySelector('.chipBalance');
                    const balText = balEl ? balEl.textContent.trim() : '0';
                    let balance = 0;
                    const cleaned = balText.replace(/[^0-9kK.]/g, '');
                    if (cleaned.toLowerCase().endsWith('k')) {
                        balance = Math.round(parseFloat(cleaned.slice(0, -1)) * 1000);
                    } else if (cleaned) {
                        balance = parseInt(cleaned) || 0;
                    }

                    if (balance < 200) {
                        resolve({success: false, error: 'balance < 200', balance: balance});
                        return;
                    }

                    // 2. Check WS connection (again, after ensure_ws_connected)
                    if (!window.connection || !connection.ws || connection.ws.readyState !== 1) {
                        resolve({success: false, error: 'ws not connected', balance: balance});
                        return;
                    }

                    // 3. Send TRANSFER
                    const msg = new OutboundMessage("TRANSFER");
                    msg.writeLong(destId);
                    msg.writeLong(balance);
                    let resolved = false;
                    connection.send(msg, function(resp, ok) {
                        if (resolved) return;
                        resolved = true;
                        try {
                            const status = resp.readSignedByte();
                            const txt = resp.readUtf16String ? resp.readUtf16String() : '';
                            resolve({success: ok, status: status, message: txt, balance: balance, dest: destId});
                        } catch(e) {
                            resolve({success: ok, error: e.toString(), balance: balance});
                        }
                    });
                    setTimeout(() => {
                        if (!resolved) {
                            resolved = true;
                            resolve({success: false, error: 'timeout', balance: balance});
                        }
                    }, 12000);
                } catch(e) {
                    resolve({success: false, error: e.toString()});
                }
            });
        }""", dest_id)
        return result
    except Exception as e:
        return {"success": False, "error": f"evaluate error: {e}"}


def find_gf(page, max_wait=120):
    """Find game frame. Default 120s (game takes 30-60s to load)."""
    for _ in range(max_wait // 5):
        for f in page.frames:
            if "instant-bundle" in f.url and "fbsbx.com" in f.url:
                return f
        time.sleep(5)
    return None


def is_account_blocked(gf):
    """Check if any visible alert dialog says account is blocked.

    Returns True if account is blocked (skip this cookie entirely).
    """
    try:
        blocked = gf.evaluate("""() => {
            const dialogs = document.querySelectorAll('[class*="msgBox"], [class*="dialog"], [class*="Dialog"], [class*="alert"]');
            for (const d of dialogs) {
                if (d.offsetParent === null) continue;
                const txt = (d.textContent || '').toLowerCase();
                if (txt.includes('blocked') || txt.includes('khóa') || txt.includes('cấm')) {
                    return true;
                }
            }
            return false;
        }""")
        return bool(blocked)
    except Exception:
        return False


def ensure_ws_connected(gf, page, max_retries=2):
    """Check WS state, reload page if disconnected.

    Returns True if WS is now connected (after possible reload).
    """
    try:
        ws_ok = gf.evaluate("() => !!(window.connection && connection.ws && connection.ws.readyState === 1)")
        if ws_ok:
            return True
    except Exception:
        pass

    print(f"  ⚠ WS not connected — reloading page...", flush=True)

    for retry in range(max_retries):
        try:
            # Reload the parent page (game will reload too)
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(20000)  # 20s for game to load

            # Re-find game frame after reload
            new_gf = find_gf(page, max_wait=60)
            if not new_gf:
                print(f"  ⚠ Reload #{retry+1}: game frame not found", flush=True)
                continue

            # Wait for WS connection
            for ws_check in range(15):  # 45s wait
                try:
                    ws_ok = new_gf.evaluate("() => !!(window.connection && connection.ws && connection.ws.readyState === 1)")
                    if ws_ok:
                        print(f"  ✓ WS reconnected after reload #{retry+1}", flush=True)
                        return True
                except:
                    pass
                page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  ⚠ Reload #{retry+1} error: {e}", flush=True)

    print(f"  ❌ WS reconnect failed after {max_retries} reloads", flush=True)
    return False


def trigger_and_claim(gf, page):
    """v3 logic + retry + WS reconnect before each claim.

    Flow:
      1. ensure_ws_connected (reload if dead)
      2. createTable()
      3. Select radio_11 (game mode)
      4. Click input[name="CREATE"] (submit)
      5. If "not enough coin" alert appears → click "Watch video" button
      6. Send OutboundMessage("VIDEO_REWARD") via WS → server returns amount
      7. Retry once if first attempt fails
    """
    # Verify WS is connected before doing anything (reload if dead)
    if not ensure_ws_connected(gf, page, max_retries=1):
        return {"success": False, "error": "ws reconnect failed"}
    # After possible reload, re-find gf
    gf_new = find_gf(page, max_wait=30)
    if gf_new:
        gf = gf_new

    try:
        gf.evaluate("createTable()")
    except Exception:
        pass
    time.sleep(2)

    try:
        gf.evaluate("""() => {
            const r = document.getElementById('radio_11');
            if (r) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true})); }
        }""")
    except Exception:
        pass
    time.sleep(0.5)

    try:
        gf.evaluate("""() => {
            const b = document.querySelector('input[name="CREATE"]');
            if (b) b.click();
        }""")
    except Exception:
        pass
    time.sleep(3)

    alert_clicked = False
    try:
        alert_clicked = gf.evaluate("""() => {
            const dialogs = document.querySelectorAll('[class*="msgBox"]');
            for (const d of dialogs) {
                if (d.offsetParent !== null && d.textContent.includes('enough coin')) {
                    const buttons = d.querySelectorAll('input[type="button"], button');
                    for (const b of buttons) {
                        const val = (b.value || b.textContent || '').toLowerCase();
                        if (val.includes('watch') || val.includes('video')) {
                            b.click();
                            return true;
                        }
                    }
                }
            }
            return false;
        }""")
    except Exception:
        pass

    if alert_clicked:
        time.sleep(2)

    # Retry logic: try + 1 retry = 2 total attempts
    max_attempts = 2
    timeout_ms = 15000
    result = None
    for attempt in range(max_attempts):
        try:
            result = gf.evaluate(f"""() => {{
                return new Promise((resolve) => {{
                    try {{
                        if (!window.connection || !connection.ws || connection.ws.readyState !== 1) {{
                            resolve({{success: false, error: 'ws not connected'}});
                            return;
                        }}
                        const msg = new OutboundMessage("VIDEO_REWARD");
                        msg.writeByte(1);
                        let resolved = false;
                        connection.send(msg, function(response, success) {{
                            if (resolved) return;
                            resolved = true;
                            if (success) {{
                                try {{
                                    const amount = response.readLong();
                                    if (window.Ads && window.Ads.RewardedVideo) {{
                                        window.Ads.RewardedVideo.videoIndex++;
                                        if (window.Ads.RewardedVideo.updateRewardButton)
                                            window.Ads.RewardedVideo.updateRewardButton();
                                    }}
                                    resolve({{success: true, amount: amount}});
                                }} catch(e) {{
                                    resolve({{success: true, amount: 0, error: e.toString()}});
                                }}
                            }} else {{
                                resolve({{success: false, error: 'no response'}});
                            }}
                        }});
                        setTimeout(() => {{
                            if (!resolved) {{ resolved = true; resolve({{success: false, error: 'timeout'}}); }}
                        }}, {timeout_ms});
                    }} catch(e) {{
                        resolve({{success: false, error: e.toString()}});
                    }}
                }});
            }}""")
        except Exception as e:
            result = {"success": False, "error": f"evaluate error: {e}"}

        # Check result — if success, break out of retry loop
        if result.get('success') and result.get('amount', 0) > 0:
            break

        # If not last attempt, log + try to recover WS before retry
        if attempt < max_attempts - 1:
            err = result.get('error', 'unknown')
            print(f"  attempt {attempt+1}/{max_attempts}: FAIL ({err}), retrying...", flush=True)
            # Close stuck msgBox dialogs
            try:
                gf.evaluate("$('.msgBoxBackGround,.msgBox').remove()")
            except:
                pass
            time.sleep(2)
            # Re-ensure WS connected (reload if needed)
            if not ensure_ws_connected(gf, page, max_retries=1):
                print(f"  WS still dead, skip retry", flush=True)
                break
            # Re-find gf after reload
            gf_new = find_gf(page, max_wait=30)
            if gf_new:
                gf = gf_new
            # Re-click watch video button
            try:
                gf.evaluate("""() => {
                    const dialogs = document.querySelectorAll('[class*="msgBox"]');
                    for (const d of dialogs) {
                        if (d.offsetParent !== null && d.textContent.includes('enough coin')) {
                            const buttons = d.querySelectorAll('input[type="button"], button');
                            for (const b of buttons) {
                                const val = (b.value || b.textContent || '').toLowerCase();
                                if (val.includes('watch') || val.includes('video')) {
                                    b.click();
                                    return true;
                                }
                            }
                        }
                    }
                    return false;
                }""")
            except:
                pass
            time.sleep(1)
        else:
            err = result.get('error', 'unknown')
            print(f"  attempt {attempt+1}/{max_attempts}: FAIL ({err}) — giving up", flush=True)

    if not result.get('success') or result.get('amount', 0) == 0:
        result['method'] = 'alert_clicked' if alert_clicked else 'no_alert'

    return result


# ============================================================
# CONTINUOUS SESSION — login + load game 1 LẦN, sau đó loop claim↔transfer
# ============================================================
def run_continuous_session(p, fb_cookies, session_id, started_at):
    """Mở browser → login FB → load game → (claim → transfer → claim → ...) LIÊN TỤC
    KHÔNG close browser, KHÔNG reload, KHÔNG login lại giữa các claim.
    Trả về (total_reward, ok, fail, cookies_ok).
    """
    print(f"\n########## SESSION {session_id} - CONTINUOUS MODE ##########", flush=True)

    # ===== [ANTI-BAN] Fingerprint cố định per-cookie =====
    fp = al.get_or_create_fingerprint(SINGLE_COOKIE_FILE) if al else None
    print(f"[ANTI-BAN] Fingerprint: UA={(fp['user_agent'][:60]+'...') if fp else 'default'} "
          f"viewport={fp['viewport'] if fp else 'default'}", flush=True)

    browser = p.chromium.launch(
        headless=HEADLESS,
        args=al.stealth_launch_args() if al else ["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )

    context_kwargs = {
        "viewport": fp["viewport"] if fp else {"width": 1920, "height": 1080},
        "locale":   fp["locale"]   if fp else "en-US",
        "user_agent": fp["user_agent"] if fp else ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/139.0.0.0 Safari/537.36"),
        "timezone_id": fp["timezone"] if fp else None,
        "color_scheme": fp.get("color_scheme", "light") if fp else "light",
    }
    context_kwargs = {k: v for k, v in context_kwargs.items() if v is not None}
    context = browser.new_context(**context_kwargs)

    # [ANTI-BAN] Inject stealth JS
    if al and fp:
        try:
            context.add_init_script(al.build_stealth_js(fp))
            print("[ANTI-BAN] Stealth JS injected OK", flush=True)
        except Exception as e:
            print(f"[ANTI-BAN] Stealth JS inject fail: {e}", flush=True)

    try:
        for c in fb_cookies:
            c['domain'] = '.facebook.com'
        context.add_cookies(fb_cookies)
    except Exception as e:
        print(f"  ERROR add_cookies: {e}", flush=True)
        try: browser.close()
        except: pass
        return 0, 0, 0, False

    page = context.new_page()

    # ===== [1] Login FB — 1 LẦN DUY NHẤT =====
    print("[1] Login FB...", flush=True)
    try:
        page.goto("https://www.facebook.com/",
                  wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  ERROR goto FB: {e}", flush=True)
        try: browser.close()
        except: pass
        return 0, 0, 0, False

    page.wait_for_timeout(5000)

    try:
        if page.locator('input[placeholder="Email or phone"]').count() > 0:
            print("  ERROR: Not logged in (cookie hết hạn?)", flush=True)
            try: browser.close()
            except: pass
            return 0, 0, 0, False
    except Exception:
        pass
    print("  OK", flush=True)

    # ===== [ANTI-BAN] Warm-up account: scroll FB feed trước khi vào game =====
    if WARMUP_ENABLED and al:
        print("[ANTI-BAN] Warm-up: scroll FB feed...", flush=True)
        try:
            al.warm_up_account(page, max_s=15)
            print("  Warm-up OK", flush=True)
        except Exception as e:
            print(f"  Warm-up fail (ignore): {e}", flush=True)

    # ===== [2] Open game — 1 LẦN DUY NHẤT =====
    print("[2] Open game...", flush=True)
    try:
        page.goto(GAME_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"  ERROR goto game: {e}", flush=True)
        try: browser.close()
        except: pass
        return 0, 0, 0, True

    page.wait_for_timeout(20000)
    gf = find_gf(page, max_wait=60)
    if not gf:
        print("  ERROR: Game frame not found", flush=True)
        try: browser.close()
        except: pass
        return 0, 0, 0, True
    print("  Game loaded", flush=True)
    page.wait_for_timeout(10000)

    # ===== [3] Wait WS — 1 LẦN =====
    print("[3] Wait WS...", flush=True)
    for _ in range(10):
        try:
            if gf.evaluate(
                "() => window.connection && connection.ws && connection.ws.readyState === 1"
            ):
                print("  WS connected", flush=True)
                break
        except Exception:
            pass
        time.sleep(3)

    # ===== Check account blocked — 1 LẦN =====
    if is_account_blocked(gf):
        print(f"  ❌ ACCOUNT BLOCKED — skipping", flush=True)
        try:
            gf.evaluate("""() => {
                const btns = document.querySelectorAll('input[type="button"], button');
                for (const b of btns) {
                    const t = (b.value || b.textContent || '').toLowerCase().trim();
                    if (t === 'ok' || t === 'đóng' || t === 'close') {
                        b.click();
                    }
                }
            }""")
        except: pass
        try: browser.close()
        except: pass
        return 0, 0, 0, False

    bal_start = get_bal(gf)
    print(f"  Balance: {bal_start}", flush=True)

    # ===== [ANTI-BAN] Daily quota check =====
    if al:
        ok_q, why = al.check_daily_quota(
            SINGLE_COOKIE_FILE,
            max_claims=MAX_CLAIMS_PER_DAY,
            max_transfers=MAX_TRANSFERS_PER_DAY,
            max_amount=MAX_TRANSFER_AMOUNT_PER_DAY,
        )
        if not ok_q:
            print(f"  ⏸ [ANTI-BAN] Bỏ qua session: {why}", flush=True)
            try: browser.close()
            except: pass
            return 0, 0, 0, True
        al.record_session_start(SINGLE_COOKIE_FILE)

    # dest_id cố định theo env TRANSFER_DEST_ID (set trong .github/workflows/ckN.yml)
    # KHÔNG xoay vòng — mỗi cookie có 1 hub account riêng theo yml.
    dest_id = TRANSFER_DEST_ID

    # ===== Pre-claim transfer (1 LẦN) =====
    bal_start_num = parse_balance_num(bal_start)
    if TRANSFER_ENABLED and bal_start_num > PRE_CLAIM_TRANSFER_THRESHOLD:
        print(f"\n[Pre-claim] Balance {bal_start_num:,} > {PRE_CLAIM_TRANSFER_THRESHOLD:,}, "
              f"transferring first to {dest_id}...", flush=True)
        try:
            pre_transfer_result = transfer_all_xu(gf, page, dest_id)
            if pre_transfer_result.get('success'):
                amt = pre_transfer_result.get('balance', 0)
                msg = pre_transfer_result.get('message', '')
                print(f"  ✅ Pre-claim transfer: {amt:,} xu → {dest_id}", flush=True)
                if msg:
                    print(f"     Server: {msg[:80]}", flush=True)
                # [ANTI-BAN] Soft-ban detect + record transfer
                if al:
                    if al.detect_soft_ban(msg) or al.detect_soft_ban(pre_transfer_result.get('error','')):
                        print("  🚫 [ANTI-BAN] Soft-ban detected — back-off 15 phút", flush=True)
                        al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                        try: browser.close()
                        except: pass
                        return 0, 0, 0, True
                    al.record_transfer(SINGLE_COOKIE_FILE, amt)
                time.sleep(2)
                bal_start = get_bal(gf)
                print(f"     Balance after pre-transfer: {bal_start}", flush=True)
            else:
                err = pre_transfer_result.get('error', 'unknown')
                print(f"  ❌ Pre-claim transfer fail: {err}", flush=True)
                msg = pre_transfer_result.get('message', '')
                if msg:
                    print(f"     Server: {msg[:80]}", flush=True)
        except Exception as e:
            print(f"  ❌ Pre-claim transfer exception: {e}", flush=True)
    elif bal_start_num > 0:
        print(f"  (Balance {bal_start_num:,} ≤ {PRE_CLAIM_TRANSFER_THRESHOLD:,}, "
              f"skip pre-claim transfer)", flush=True)

    # ===== [4] BATCH LOOP: claim CLAIM_BATCH times → transfer → repeat =====
    print(f"\n[4] BATCH MODE: claim ~{CLAIM_BATCH} (jitter) → transfer → repeat...", flush=True)
    total_reward = 0
    total_transferred = 0
    ok = 0
    fail = 0
    claim_count = 0

    # Outer loop: claim CLAIM_BATCH times → transfer → repeat
    while True:
        elapsed = time.time() - started_at
        if elapsed > MAX_RUNTIME:
            print(f"  Hết thời gian ({MAX_RUNTIME}s), dừng.", flush=True)
            break

        # dest_id lấy từ TRANSFER_DEST_ID env (đã set đầu hàm, không đổi)
        # [ANTI-BAN] Batch size jitter (CLAIM_BATCH±15 thay vì fixed 40)
        cur_batch = al.random_batch_size(CLAIM_BATCH, lo=max(10, CLAIM_BATCH-5), hi=CLAIM_BATCH+5) if al else CLAIM_BATCH

        # Check pre-claim balance and transfer if needed
        bal_before_batch = get_bal(gf)
        bal_before_batch_num = parse_balance_num(bal_before_batch)
        if TRANSFER_ENABLED and bal_before_batch_num > PRE_CLAIM_TRANSFER_THRESHOLD:
            print(f"\n[BATCH START] Balance: {bal_before_batch} → Pre-batch transfer to {dest_id}...", flush=True)
            try:
                pre_result = transfer_all_xu(gf, page, dest_id)
                if pre_result.get('success'):
                    amt = pre_result.get('balance', 0)
                    msg = pre_result.get('message', '')
                    print(f"  ✅ Pre-batch transfer: {amt:,} xu → {dest_id}", flush=True)
                    # [ANTI-BAN] Soft-ban detect + record
                    if al:
                        if al.detect_soft_ban(msg) or al.detect_soft_ban(pre_result.get('error','')):
                            print("  🚫 [ANTI-BAN] Soft-ban detected — back-off 15 phút", flush=True)
                            al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                            break
                        al.record_transfer(SINGLE_COOKIE_FILE, amt)
                    time.sleep(2)
            except Exception as e:
                print(f"  ❌ Pre-batch transfer fail: {e}", flush=True)

        # ===== INNER BATCH LOOP: claim cur_batch times =====
        print(f"\n[Claim Batch #{claim_count // max(cur_batch,1) + 1}] Starting {cur_batch} claims (dest={dest_id})...", flush=True)

        for batch_idx in range(cur_batch):
            elapsed = time.time() - started_at
            if elapsed > MAX_RUNTIME:
                print(f"  Hết thời gian ({MAX_RUNTIME}s) trong quá trình claim, dừng.", flush=True)
                break

            bal_before = get_bal(gf)

            # ===== CLAIM =====
            print(f"  [Claim #{claim_count + batch_idx + 1}] Balance: {bal_before} → Claiming...", flush=True)
            try:
                gf.evaluate("$('.msgBoxBackGround,.msgBox').remove()")
            except Exception:
                pass
            al.jitter_sleep(1.0, 1.0) if al else time.sleep(1)

            try:
                result = trigger_and_claim(gf, page)
            except Exception as e:
                print(f"    EXCEPTION ({e})", flush=True)
                fail += 1
                if fail >= 8:
                    break
                if al: al.human_delay(max(2.0, DELAY), max(4.0, DELAY+3.0))
                else:  time.sleep(DELAY)
                continue

            # [ANTI-BAN] Soft-ban detect trong response
            if al and (al.detect_soft_ban(result.get('error', '')) or
                       al.detect_soft_ban(result.get('message', ''))):
                print("  🚫 [ANTI-BAN] Soft-ban detected — back-off 15 phút", flush=True)
                al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                break

            if result.get('success') and result.get('amount', 0) > 0:
                amount = result['amount']
                total_reward += amount
                ok += 1
                claim_count += 1
                fail = 0
                if al: al.record_claim(SINGLE_COOKIE_FILE, amount)
                # [ANTI-BAN] Human-like delay
                al.jitter_sleep(1.0, 0.8) if al else time.sleep(1)
                bal_after_claim = get_bal(gf)
                print(f"    ✅ Claim #{claim_count} OK +{amount} | {bal_before} -> {bal_after_claim} "
                      f"| total reward={total_reward}", flush=True)

                # [ANTI-BAN] Idle break mỗi IDLE_EVERY_CLAIMS claim
                if al and IDLE_EVERY_CLAIMS > 0:
                    try:
                        al.maybe_idle_browse(page, claim_count, every=IDLE_EVERY_CLAIMS)
                    except Exception:
                        pass
            else:
                fail += 1
                err = result.get('error', 'unknown')
                method = result.get('method', '')
                print(f"    ❌ Claim FAIL ({err}) [{method}] | {bal_before}", flush=True)

                # [GAME-RULE] Phát hiện fail vì balance cao (>50k — game chặn claim khi >55k)
                # → ép transfer ngay, không chơi tiếp vô ích
                cur_bal_num = parse_balance_num(get_bal(gf))
                if cur_bal_num >= 50000:
                    print(f"  💸 Balance {cur_bal_num:,} >= 50k — game chặn claim, ÉP TRANSFER", flush=True)
                    break

            if fail >= 8:
                print("  Too many fails, stopping batch", flush=True)
                break

            # [ANTI-BAN] Human-like delay giữa các claim (Poisson 2-6s)
            if al: al.human_delay(max(2.0, DELAY), max(4.0, DELAY+3.0))
            else:  time.sleep(DELAY)

        if fail >= 8:
            break

        # ===== TRANSFER (sau khi claim hết batch) =====
        bal_after_batch = get_bal(gf)
        bal_after_batch_num = parse_balance_num(bal_after_batch)

        if TRANSFER_ENABLED and bal_after_batch_num > 200:
            print(f"\n[Transfer] Balance: {bal_after_batch} → Transferring to {dest_id}...", flush=True)
            try:
                transfer_result = transfer_all_xu(gf, page, dest_id)
                if transfer_result.get('success'):
                    amt = transfer_result.get('balance', 0)
                    msg = transfer_result.get('message', '')
                    total_transferred += amt
                    print(f"  ✅ Transferred {amt:,} xu → {dest_id}", flush=True)
                    if msg:
                        print(f"     Server: {msg[:80]}", flush=True)
                    # [ANTI-BAN] Record + soft-ban detect
                    if al:
                        if al.detect_soft_ban(msg) or al.detect_soft_ban(transfer_result.get('error','')):
                            print("  🚫 [ANTI-BAN] Soft-ban detected — back-off 15 phút", flush=True)
                            al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                            break
                        al.record_transfer(SINGLE_COOKIE_FILE, amt)
                    time.sleep(2)
                    bal_after_transfer = get_bal(gf)
                    print(f"     Balance after transfer: {bal_after_transfer}", flush=True)
                else:
                    err = transfer_result.get('error', 'unknown')
                    print(f"  ❌ Transfer FAIL: {err}", flush=True)
                    msg = transfer_result.get('message', '')
                    if msg:
                        print(f"     Server: {msg[:80]}", flush=True)
                    # [ANTI-BAN] Soft-ban detect trong fail
                    if al and al.detect_soft_ban(err):
                        print("  🚫 [ANTI-BAN] Soft-ban detected — back-off 15 phút", flush=True)
                        al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                        break
            except Exception as e:
                print(f"  ❌ Transfer exception: {e}", flush=True)
        else:
            print(f"  ⚠ Balance {bal_after_batch_num} ≤ 200, skip transfer", flush=True)

        # [ANTI-BAN] Rest giữa 2 batch cũng nên có jitter
        if al: al.human_delay(max(3.0, DELAY), max(8.0, DELAY+5.0))
        else:  time.sleep(DELAY)

    print(f"\n[SESSION {session_id}] Xong | claims OK={ok} fail={fail} "
          f"| reward={total_reward:,} | transferred={total_transferred:,}", flush=True)

    # KHÔNG close browser — giữ session
    return total_reward, ok, fail, True


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("FB Tien Len Mien Nam reward bot v9 — CONTINUOUS MODE")
    print("Claim và transfer LIÊN TỤC, KHÔNG close/reload/login")
    print("=" * 60)
    print(f"Config: CLAIM_BATCH={CLAIM_BATCH} COOLDOWN={DELAY}s REST={REST}s "
          f"TRANSFER_DEST={TRANSFER_DEST_ID}", flush=True)
    print("=" * 60)

    # ★ CHỈ LOAD ĐÚNG 1 FILE COOKIE
    cookie_entries = load_single_cookie_set(SINGLE_COOKIE_FILE)
    if not cookie_entries:
        print(f"[STOP] Không nạp được cookie từ {SINGLE_COOKIE_FILE}", flush=True)
        return 1

    # Parse chuỗi cookie header thành list cookie dict cho Playwright
    entry = cookie_entries[0]
    fb_cookies = parse_cookie(entry["raw"])
    if not fb_cookies:
        print(f"[STOP] Cookie {entry['file']} parse rỗng — có thể định dạng sai.", flush=True)
        return 1
    print(f"[COOKIE] ✅ Đã parse {len(fb_cookies)} cookies từ {entry['file']}", flush=True)

    # [ANTI-BAN] Daily quota check ngay từ đầu
    if al:
        ok_q, why = al.check_daily_quota(
            SINGLE_COOKIE_FILE,
            max_claims=MAX_CLAIMS_PER_DAY,
            max_transfers=MAX_TRANSFERS_PER_DAY,
            max_amount=MAX_TRANSFER_AMOUNT_PER_DAY,
        )
        if not ok_q:
            print(f"\n[ANTI-BAN] ⏸ Dừng ngay: {why}", flush=True)
            return 0

    # [ANTI-BAN] Random session start offset để 8 cookie không đồng loạt bật cùng lúc
    if al and SESSION_START_JITTER > 0:
        wait = al.random_session_offset(SESSION_START_JITTER)
        print(f"\n[ANTI-BAN] ⏳ Session start jitter: nghỉ {wait:.1f}s trước khi mở browser...", flush=True)

    started_at = time.time()
    session_id = 0
    grand_reward = 0
    grand_ok = 0
    grand_fail = 0

    with sync_playwright() as p:
        # ★ Chỉ chạy 1 session (vì session không close browser)
        session_id += 1
        print(f"\n{'=' * 60}")
        print(f"[Session {session_id}] Starting at {time.strftime('%H:%M:%S')}", flush=True)
        print(f"{'=' * 60}")

        total, ok, fail, cookies_ok = run_continuous_session(p, fb_cookies, session_id, started_at)
        grand_reward += total
        grand_ok += ok
        grand_fail += fail

        print(f"\n[Session {session_id}] Result: {ok} claims OK, {fail} fail", flush=True)
        print(f"[Total] {grand_ok} claims OK, total reward: {grand_reward:,}", flush=True)

    print(f"\n{'=' * 60}")
    print(f"TỔNG: {session_id} session | {grand_ok} claims ok | "
          f"{grand_fail} fail | reward={grand_reward:,}", flush=True)
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
