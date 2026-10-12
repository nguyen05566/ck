#!/usr/bin/env python3
"""FB Sam loc reward bot v9 — CONTINUOUS MODE
★ Claim và transfer LIÊN TỤC, KHÔNG cần close/reload/login
Mỗi lần: claim → transfer → claim → transfer → ... cho đến hết thời gian
"""
import os, sys, time, re

# Thử import module bổ trợ nếu có (không bắt buộc)
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import board_dom_merged as m
except Exception:
    m = None

from playwright.sync_api import sync_playwright

GAME_URL = "https://www.facebook.com/gaming/play/chines-chess-vh"
MAX_CYCLES = int(os.environ.get("MAX_CLAIMS", "1"))  # Mỗi lần claim 1 lần rồi transfer
DELAY = float(os.environ.get("COOLDOWN", "3"))
REST = int(os.environ.get("REST_BETWEEN_RUNS", "5"))
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", str(330 * 60)))
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"

# ============ TRANSFER LOGIC ============
# Transfer xu về hub account 68307415 sau MỖI claim
TRANSFER_DEST_ID = int(os.environ.get("TRANSFER_DEST_ID", "68307415"))
TRANSFER_ENABLED = os.environ.get("TRANSFER_ENABLED", "true").lower() == "true"

# ============ SINGLE COOKIE MODE ============
SINGLE_COOKIE_FILE = os.environ.get("SINGLE_COOKIE_FILE", "ck15.txt").strip()

# ============ Pre-claim transfer threshold ============
PRE_CLAIM_TRANSFER_THRESHOLD = int(os.environ.get("PRE_CLAIM_TRANSFER_THRESHOLD", "10000"))


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
    if m is not None and hasattr(m, "parse_cookie_header"):
        try:
            return m.parse_cookie_header(raw)
        except Exception:
            pass
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
# Các selector có thể dùng cho balance — thử lần lượt
BALANCE_SELECTORS = [
    '.chipBalance',
    '[class*="chipBalance"]',
    '[class*="Balance"]',
    '[class*="balance"]',
    '[class*="chip"]',
    '[class*="xu"]',
    '[class*="coin"]',
    '[class*="gold"]',
    '[class*="money"]',
]


def get_bal(gf):
    """Thử nhiều selector cho tới khi tìm thấy balance."""
    for sel in BALANCE_SELECTORS:
        try:
            txt = gf.evaluate(
                f"() => (document.querySelector({repr(sel)})?.textContent || '').trim() || ''"
            )
            if txt and txt != '?' and txt != '':
                return txt
        except Exception:
            continue
    return "?"


def dump_dom_debug(gf, page, label="claim_fail"):
    """Dump DOM khi claim/transfer fail để debug selector.

    Lưu screenshot + in log:
      - Tất cả visible buttons + text + class
      - Tất cả elements có class liên quan tới balance/xu/coin
      - Tất cả visible dialogs
    """
    print(f"  🔍 DEBUG DOM DUMP ({label}):", flush=True)
    try:
        # Screenshot
        ts = int(time.time())
        shot_path = f"/tmp/debug_{label}_{ts}.png"
        try:
            page.screenshot(path=shot_path, full_page=True)
            print(f"     Screenshot: {shot_path}", flush=True)
        except Exception as e:
            print(f"     screenshot fail: {e}", flush=True)

        # All visible buttons
        try:
            btns = gf.evaluate("""() => {
                const out = [];
                document.querySelectorAll('button, input[type="button"], input[type="submit"], [role="button"], a[class*="btn"]').forEach(b => {
                    if (b.offsetParent === null) return;
                    const txt = (b.textContent || b.value || '').trim().slice(0, 60);
                    const cls = (b.className || '').toString().slice(0, 100);
                    const id = (b.id || '').slice(0, 60);
                    out.push({text: txt, class: cls, id: id, tag: b.tagName});
                });
                return out.slice(0, 60);
            }""")
            print(f"     Visible buttons ({len(btns)}):", flush=True)
            for i, b in enumerate(btns[:30]):
                print(f"       [{i}] <{b['tag']}> text={b['text']!r} class={b['class']!r} id={b['id']!r}", flush=True)
        except Exception as e:
            print(f"     buttons dump fail: {e}", flush=True)

        # Elements with balance-related classes
        try:
            bal_els = gf.evaluate("""() => {
                const out = [];
                document.querySelectorAll('[class*="balance"], [class*="Balance"], [class*="chip"], [class*="xu"], [class*="coin"], [class*="Xu"], [class*="Coin"]').forEach(el => {
                    if (el.offsetParent === null) return;
                    out.push({
                        text: (el.textContent || '').trim().slice(0, 80),
                        class: (el.className || '').toString().slice(0, 100),
                        tag: el.tagName
                    });
                });
                return out.slice(0, 30);
            }""")
            print(f"     Balance-like elements ({len(bal_els)}):", flush=True)
            for i, e in enumerate(bal_els[:15]):
                print(f"       [{i}] <{e['tag']}> text={e['text']!r} class={e['class']!r}", flush=True)
        except Exception as e:
            print(f"     balance dump fail: {e}", flush=True)

        # Visible dialogs/alerts
        try:
            dialogs = gf.evaluate("""() => {
                const out = [];
                document.querySelectorAll('[class*="msgBox"], [class*="dialog"], [class*="Dialog"], [class*="alert"], [class*="popup"], [class*="Popup"], [class*="modal"], [class*="Modal"]').forEach(d => {
                    if (d.offsetParent === null) return;
                    out.push({
                        text: (d.textContent || '').trim().slice(0, 200),
                        class: (d.className || '').toString().slice(0, 100)
                    });
                });
                return out.slice(0, 10);
            }""")
            print(f"     Visible dialogs ({len(dialogs)}):", flush=True)
            for i, d in enumerate(dialogs[:5]):
                print(f"       [{i}] class={d['class']!r} text={d['text']!r}", flush=True)
        except Exception as e:
            print(f"     dialogs dump fail: {e}", flush=True)

        # Available JS globals
        try:
            funcs = gf.evaluate("""() => {
                const names = [];
                for (const k of Object.keys(window)) {
                    if (typeof window[k] === 'function' && !k.startsWith('_')) {
                        names.push(k);
                    }
                }
                return names.slice(0, 50);
            }""")
            print(f"     Window functions ({len(funcs)}): {funcs[:30]}", flush=True)
        except Exception as e:
            print(f"     funcs dump fail: {e}", flush=True)

    except Exception as e:
        print(f"     dump_dom_debug exception: {e}", flush=True)


def transfer_all_xu(gf, page, dest_id=TRANSFER_DEST_ID):
    """Transfer ALL current xu về dest_id via game's connection.send.
    Returns dict {success, amount, status, message}.
    """
    # Ensure WS is connected (reload page if needed)
    if not ensure_ws_connected(gf, page):
        return {"success": False, "error": "ws reconnect failed after reloads"}
    gf_new = find_gf(page, max_wait=30)
    if gf_new:
        gf = gf_new

    try:
        result = gf.evaluate("""(destId, balSelectors) => {
            return new Promise((resolve) => {
                try {
                    // Thử nhiều selector cho balance
                    let balEl = null;
                    let balText = '0';
                    for (const sel of balSelectors) {
                        balEl = document.querySelector(sel);
                        if (balEl && balEl.textContent && balEl.textContent.trim()) {
                            balText = balEl.textContent.trim();
                            break;
                        }
                    }
                    let balance = 0;
                    const cleaned = balText.replace(/[^0-9kK.]/g, '');
                    if (cleaned.toLowerCase().endsWith('k')) {
                        balance = Math.round(parseFloat(cleaned.slice(0, -1)) * 1000);
                    } else if (cleaned) {
                        balance = parseInt(cleaned) || 0;
                    }

                    if (balance < 200) {
                        resolve({success: false, error: 'balance < 200', balance: balance, balText: balText});
                        return;
                    }

                    if (!window.connection || !connection.ws || connection.ws.readyState !== 1) {
                        resolve({success: false, error: 'ws not connected', balance: balance});
                        return;
                    }

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
        }""", dest_id, BALANCE_SELECTORS)
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
    """Check if any visible alert dialog says account is blocked."""
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
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(20000)
            new_gf = find_gf(page, max_wait=60)
            if not new_gf:
                print(f"  ⚠ Reload #{retry+1}: game frame not found", flush=True)
                continue
            for ws_check in range(15):
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
    return False


def trigger_and_claim(gf, page):
    """v3 logic + retry + WS reconnect before each claim."""
    if not ensure_ws_connected(gf, page, max_retries=1):
        return {"success": False, "error": "ws reconnect failed"}
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
            const btn = document.querySelector('button[class*="reward"][class*="claim"], button[class*="Claim"]');
            if (btn && btn.offsetParent !== null) {
                btn.click();
                return true;
            }
            return false;
        }""")
    except Exception:
        pass

    time.sleep(0.5)

    try:
        clicked = gf.evaluate("""() => {
            const btn = document.querySelector('button[class*="reward"][class*="claim"], button[class*="Claim"]');
            if (btn && btn.offsetParent !== null && !btn.disabled) {
                btn.click();
                return true;
            }
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
                if (b.offsetParent !== null && !b.disabled) {
                    const txt = (b.textContent || '').toLowerCase();
                    if (txt.includes('claim') || txt.includes('nhận') || txt.includes('reward') || txt.includes('lấy')) {
                        b.click();
                        return true;
                    }
                }
            }
            return false;
        }""")
        if not clicked:
            # ★ Dump DOM để debug — selector cho Sam Loc có thể không khớp Tien Len
            dump_dom_debug(gf, page, label="claim_btn_not_found")
            return {"success": False, "error": "claim button not found (dumped DOM for debug)"}
    except Exception as e:
        return {"success": False, "error": f"click error: {e}"}

    time.sleep(3)

    for _ in range(10):
        try:
            status = gf.evaluate("""() => {
                const dialog = document.querySelector('[class*="msgBox"][class*="dialog"]');
                if (dialog && dialog.offsetParent !== null) {
                    const txt = (dialog.textContent || '').toLowerCase();
                    if (txt.includes('success') || txt.includes('thành công') || txt.includes('nhận')) {
                        return 'ok';
                    }
                    if (txt.includes('error') || txt.includes('lỗi') || txt.includes('fail')) {
                        return 'fail';
                    }
                }
                return 'waiting';
            }""")
            if status == 'ok':
                time.sleep(1)
                return {"success": True}
            if status == 'fail':
                return {"success": False, "error": "claim failed"}
        except Exception:
            pass
        time.sleep(1)

    return {"success": False, "error": "claim timeout"}


def run_continuous_session(p, fb_cookies, session_id, started_at):
    """Mở browser → login → game → (claim → transfer → claim → transfer → ...) LIÊN TỤC
    KHÔNG close browser, KHÔNG reload, KHÔNG login lại
    Trả về (total, ok, fail, cookies_ok).
    """
    print(f"\n########## SESSION {session_id} - CONTINUOUS MODE ##########", flush=True)

    browser = p.chromium.launch(
        headless=HEADLESS,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080}, locale="en-US",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/139.0.0.0 Safari/537.36",
    )

    try:
        for c in fb_cookies:
            c['domain'] = '.facebook.com'
        context.add_cookies(fb_cookies)
    except Exception as e:
        print(f"  ERROR add_cookies: {e}", flush=True)
        try:
            browser.close()
        except Exception:
            pass
        return 0, 0, 0, False

    page = context.new_page()

    # ===== Login check =====
    print("[1] Login FB...", flush=True)
    try:
        page.goto("https://www.facebook.com/",
                  wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  ERROR goto FB: {e}", flush=True)
        try:
            browser.close()
        except Exception:
            pass
        return 0, 0, 0, False

    page.wait_for_timeout(5000)

    try:
        if page.locator('input[placeholder="Email or phone"]').count() > 0:
            print("  ERROR: Not logged in (cookie hết hạn?)", flush=True)
            browser.close()
            return 0, 0, 0, False
    except Exception:
        pass
    print("  OK", flush=True)

    # ===== Open game =====
    print("[2] Open game...", flush=True)
    try:
        page.goto(GAME_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"  ERROR goto game: {e}", flush=True)
        try:
            browser.close()
        except Exception:
            pass
        return 0, 0, 0, True

    page.wait_for_timeout(20000)
    gf = find_gf(page, max_wait=60)
    if not gf:
        print("  ERROR: Game frame not found", flush=True)
        try:
            browser.close()
        except Exception:
            pass
        return 0, 0, 0, True
    print("  Game loaded", flush=True)
    page.wait_for_timeout(10000)

    # ===== Wait WS =====
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

    # ===== Check if account is blocked =====
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

    # ===== CONTINUOUS LOOP: claim → transfer → claim → transfer → ... =====
    print(f"\n[4] CONTINUOUS MODE: claim → transfer → claim → transfer → ...", flush=True)
    total = 0
    ok = 0
    fail = 0
    claim_count = 0

    while True:
        elapsed = time.time() - started_at
        if elapsed > MAX_RUNTIME:
            print("  Hết thời gian cho phép, dừng.", flush=True)
            break

        bal_before = get_bal(gf)
        bal_before_num = parse_balance_num(bal_before)
        
        # ===== CLAIM =====
        print(f"\n[Claim #{claim_count + 1}] Balance: {bal_before} → Claiming...", flush=True)
        try:
            gf.evaluate("$('.msgBoxBackGround,.msgBox').remove()")
        except Exception:
            pass
        time.sleep(1)

        try:
            result = trigger_and_claim(gf, page)
        except Exception as e:
            print(f"  EXCEPTION ({e})", flush=True)
            fail += 1
            if fail >= 8:
                break
            time.sleep(DELAY)
            continue

        if result.get('success'):
            ok += 1
            claim_count += 1
            time.sleep(1)
            bal_after_claim = get_bal(gf)
            print(f"  ✅ Claim OK! | {bal_before} -> {bal_after_claim}", flush=True)
            fail = 0
        else:
            fail += 1
            err = result.get('error', 'unknown')
            print(f"  ❌ Claim FAIL ({err}) | {bal_before}", flush=True)

        if fail >= 8:
            print("  Too many fails, stopping", flush=True)
            break

        time.sleep(DELAY)

        # ===== TRANSFER =====
        bal_after_claim = get_bal(gf)
        bal_after_num = parse_balance_num(bal_after_claim)
        
        if TRANSFER_ENABLED and bal_after_num > 200:
            print(f"\n[Transfer #{claim_count}] Balance: {bal_after_claim} → Transferring...", flush=True)
            try:
                transfer_result = transfer_all_xu(gf, page, TRANSFER_DEST_ID)
                if transfer_result.get('success'):
                    amt = transfer_result.get('balance', 0)
                    msg = transfer_result.get('message', '')
                    print(f"  ✅ Transferred {amt:,} xu → {TRANSFER_DEST_ID}", flush=True)
                    if msg:
                        print(f"     Server: {msg[:80]}", flush=True)
                    time.sleep(2)
                    bal_after_transfer = get_bal(gf)
                    print(f"     Balance after transfer: {bal_after_transfer}", flush=True)
                    total += amt
                else:
                    err = transfer_result.get('error', 'unknown')
                    print(f"  ❌ Transfer FAIL: {err}", flush=True)
                    msg = transfer_result.get('message', '')
                    if msg:
                        print(f"     Server: {msg[:80]}", flush=True)
            except Exception as e:
                print(f"  ❌ Transfer exception: {e}", flush=True)
        else:
            print(f"  ⚠ Balance {bal_after_num} ≤ 200, skip transfer", flush=True)

        time.sleep(DELAY)

    print(f"\n[SESSION {session_id}] Xong | ok={ok} fail={fail} | total transferred={total}", flush=True)

    # KHÔNG close browser - giữ session cho lần sau
    # browser.close()  # ← ĐÃ BỎ DÒNG NÀY
    return total, ok, fail, True


def main():
    print("=" * 60)
    print("FB Sam loc reward bot v9 — CONTINUOUS MODE")
    print("Claim và transfer LIÊN TỤC, KHÔNG close/reload/login")
    print("=" * 60)
    print(f"Config: MAX_CLAIMS={MAX_CYCLES} COOLDOWN={DELAY}s REST={REST}s "
          f"TRANSFER_DEST={TRANSFER_DEST_ID}", flush=True)
    print("=" * 60)

    cookie_entries = load_single_cookie_set(SINGLE_COOKIE_FILE)
    if not cookie_entries:
        print("No valid cookie file found. Exiting.", flush=True)
        sys.exit(1)

    # ★ Parse chuỗi cookie header thành list cookie dict cho Playwright
    # (load_single_cookie_set trả về [{"file":..., "raw":...}], chưa parse)
    entry = cookie_entries[0]
    fb_cookies = parse_cookie(entry["raw"])
    if not fb_cookies:
        print(f"[STOP] Cookie {entry['file']} parse rỗng — có thể định dạng sai.", flush=True)
        sys.exit(1)
    print(f"[COOKIE] ✅ Đã parse {len(fb_cookies)} cookies từ {entry['file']}", flush=True)

    global_start = time.time()
    session_id = 0
    grand_total = 0
    grand_ok = 0
    grand_fail = 0

    with sync_playwright() as p:
        # Chỉ chạy 1 session (vì session không close browser)
        session_id += 1
        print(f"\n{'=' * 60}")
        print(f"[Session {session_id}] Starting at {time.strftime('%H:%M:%S')}", flush=True)
        print(f"{'=' * 60}")

        total, ok, fail, cookies_ok = run_continuous_session(p, fb_cookies, session_id, global_start)
        grand_total += total
        grand_ok += ok
        grand_fail += fail

        print(f"\n[Session {session_id}] Result: {ok} transfers OK, {fail} fail", flush=True)
        print(f"[Total] {grand_ok} transfers OK, total xu: {grand_total:,}", flush=True)

    print(f"\n{'=' * 60}")
    print(f"TỔNG: {session_id} session | {grand_ok} transfers ok | "
          f"{grand_fail} fail | {grand_total:,} xu transferred", flush=True)
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
