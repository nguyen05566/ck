#!/usr/bin/env python3
"""
FB Tien Len Mien Nam reward bot v10 — BATCH TRANSFER + ROBUST RECOVERY

Khác biệt so với v9 (sửa lỗi lặp vô hạn "frame not found" / "ws disconnect"):
  1. GameSession bọc page + frame: mọi evaluate() đi qua .eval(), tự dò lại frame
     khi frame bị detach / execution context destroyed (không giữ frame cũ đã chết).
  2. recover() tập trung 1 chỗ: có COOLDOWN giữa 2 lần reload, giới hạn tổng số
     reload / số lần recover thất bại liên tiếp -> ném SessionDead thay vì loop mãi.
  3. Luân phiên reload() và goto() (hard navigate) khi khôi phục.
  4. Phát hiện logout / checkpoint / page closed -> kết thúc session ngay.
  5. Khi session chết: đóng browser sạch sẽ, nghỉ REST, mở session MỚI nếu còn thời gian
     (trước kia browser bị giữ mãi và vòng lặp cứ fail liên tục).
  6. Giới hạn fail liên tiếp của claim -> thử recover 1 lần -> vẫn fail thì kết thúc session.
"""

import os, sys, time, random

# Module bổ trợ (không bắt buộc)
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import board_dom_merged as m
except Exception:
    m = None

# === Module chống khóa nick (anti-ban) ===
# Cung cấp: fingerprint per-account, stealth JS, human-like timing,
# daily quota, soft-ban detection, transfer dest rotation.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import anti_lock as al
except Exception as e:
    print(f"[WARN] Không nạp được anti_lock.py — chạy không có chống khóa nick: {e}", flush=True)
    al = None

from playwright.sync_api import sync_playwright

# ============================================================
# CONFIG
# ============================================================
GAME_URL = "https://www.facebook.com/gaming/play/mystery-xiangqi"

CLAIM_BATCH   = int(os.environ.get("CLAIM_BATCH", "40"))          # claim bao nhiêu lần rồi transfer
DELAY         = float(os.environ.get("COOLDOWN", "3"))            # nghỉ giữa 2 lần claim
REST          = int(os.environ.get("REST_BETWEEN_RUNS", "30"))    # nghỉ giữa 2 session
MAX_RUNTIME   = int(os.environ.get("MAX_RUNTIME", str(330 * 60)))
HEADLESS      = os.environ.get("HEADLESS", "true").lower() == "true"

TRANSFER_DEST_ID = int(os.environ.get("TRANSFER_DEST_ID", "51977054"))
TRANSFER_ENABLED = os.environ.get("TRANSFER_ENABLED", "true").lower() == "true"
PRE_CLAIM_TRANSFER_THRESHOLD = int(os.environ.get("PRE_CLAIM_TRANSFER_THRESHOLD", "10000"))

SINGLE_COOKIE_FILE = os.environ.get("SINGLE_COOKIE_FILE", "ck15.txt").strip()

# ---- Chống loop vô hạn ----
MAX_SESSIONS            = int(os.environ.get("MAX_SESSIONS", "12"))          # số lần mở lại browser
MAX_RELOADS_PER_SESSION = int(os.environ.get("MAX_RELOADS", "8"))            # số lần reload/goto trong 1 session
MAX_RECOVER_FAIL        = int(os.environ.get("MAX_RECOVER_FAIL", "3"))       # recover fail liên tiếp -> bỏ session
RELOAD_COOLDOWN         = int(os.environ.get("RELOAD_COOLDOWN", "45"))       # giây tối thiểu giữa 2 lần reload
FRAME_WAIT              = int(os.environ.get("FRAME_WAIT", "90"))            # giây chờ game frame
WS_WAIT                 = int(os.environ.get("WS_WAIT", "60"))               # giây chờ websocket
MAX_CONSEC_CLAIM_FAIL   = int(os.environ.get("MAX_CONSEC_CLAIM_FAIL", "6"))  # claim fail liên tiếp
MIN_TIME_LEFT           = 90                                                  # còn < 90s thì không bày trò nữa

# ---- Quota hàng ngày (chống spam server) ----
MAX_CLAIMS_PER_DAY         = int(os.environ.get("MAX_CLAIMS_PER_DAY", str(getattr(al, "DEFAULT_MAX_CLAIMS_PER_DAY", 180) if al else 180)))
MAX_TRANSFERS_PER_DAY      = int(os.environ.get("MAX_TRANSFERS_PER_DAY", str(getattr(al, "DEFAULT_MAX_TRANSFERS_PER_DAY", 30) if al else 30)))
MAX_TRANSFER_AMOUNT_PER_DAY = int(os.environ.get("MAX_TRANSFER_AMOUNT_PER_DAY", str(getattr(al, "DEFAULT_MAX_TRANSFER_AMOUNT", 2_000_000) if al else 2_000_000)))

# ---- Random session start offset (giây) ----
# Tránh 8 cookie đồng loạt bật browser cùng lúc.
SESSION_START_JITTER = int(os.environ.get("SESSION_START_JITTER", "180"))

# ---- Idle break mỗi N claim ----
IDLE_EVERY_CLAIMS = int(os.environ.get("IDLE_EVERY_CLAIMS", "60"))

# ---- Có bật warm-up account trước khi vào game không? ----
WARMUP_ENABLED = os.environ.get("WARMUP_ENABLED", "true").lower() == "true"

FRAME_ERR_HINTS = (
    "detach", "execution context", "target closed", "has been closed",
    "frame was", "navigation", "page closed", "browser has been closed",
    "connection closed",
)


def log(msg=""):
    print(msg, flush=True)


def is_frame_error(err_text: str) -> bool:
    t = (err_text or "").lower()
    return any(h in t for h in FRAME_ERR_HINTS)


class SessionDead(Exception):
    """Session không thể cứu được -> đóng browser, mở session mới."""
    pass


# ============================================================
# COOKIE
# ============================================================
def load_single_cookie_set(path):
    if not os.path.exists(path):
        log(f"[COOKIE] ❌ Không tìm thấy file: {path}")
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read().strip()
    except Exception as e:
        log(f"[COOKIE] ❌ Lỗi đọc {path}: {e}")
        return []
    if not content:
        log(f"[COOKIE] ❌ {path} rỗng")
        return []
    content = content.strip('"').strip("'")
    content = " ".join(content.split())
    content = content.replace(";  ", "; ").replace(" ;", ";")
    log(f"[COOKIE] ✅ Nạp {os.path.basename(path)} ({len(content)} ký tự)")
    return [{"file": os.path.basename(path), "raw": content}]


def parse_cookie(raw: str):
    raw = raw.strip().strip('"').strip("'")
    raw = " ".join(raw.split())
    raw = raw.replace(";  ", "; ").replace(" ;", ";")
    if m is not None and hasattr(m, "parse_cookie_header"):
        try:
            out = m.parse_cookie_header(raw)
            if out:
                return out
        except Exception:
            pass
    import http.cookies
    parsed = http.cookies.SimpleCookie()
    try:
        parsed.load(raw)
    except Exception:
        parsed = None
    if parsed:
        out = [
            {"name": n, "value": mv.value, "domain": ".facebook.com",
             "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax"}
            for n, mv in parsed.items() if n and mv.value
        ]
        if out:
            return out
    # Fallback thô: tách thủ công
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        n, v = part.split("=", 1)
        n, v = n.strip(), v.strip()
        if n and v:
            out.append({"name": n, "value": v, "domain": ".facebook.com",
                        "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax"})
    return out


def parse_balance_num(bal_text):
    """'56.4k' / '123,456' / '78900' -> int"""
    if not bal_text or bal_text == "?":
        return 0
    s = str(bal_text).strip().lower().replace(",", "").replace(" ", "")
    s = "".join(ch for ch in s if ch.isdigit() or ch in ".km")
    try:
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1000000)
        return int(float(s))
    except Exception:
        return 0


# ============================================================
# GAME SESSION (bọc page + frame + logic khôi phục)
# ============================================================
class GameSession:
    def __init__(self, page, started_at):
        self.page = page
        self.started_at = started_at
        self.gf = None
        self.reloads = 0
        self.recover_fail = 0
        self.last_reload_ts = 0.0

    # ---------- time ----------
    def time_left(self):
        return MAX_RUNTIME - (time.time() - self.started_at)

    def check_time(self):
        if self.time_left() <= MIN_TIME_LEFT:
            raise SessionDead("hết thời gian MAX_RUNTIME")

    # ---------- frame ----------
    def _frame_alive(self, f):
        if f is None:
            return False
        try:
            if f.is_detached():
                return False
        except Exception:
            return False
        try:
            f.evaluate("() => 1")
            return True
        except Exception:
            return False

    def _scan_frames(self):
        try:
            frames = list(self.page.frames)
        except Exception:
            return None
        # 1) khớp URL bundle game
        for f in frames:
            try:
                if f.is_detached():
                    continue
                u = f.url or ""
            except Exception:
                continue
            if "instant-bundle" in u and "fbsbx.com" in u:
                if self._frame_alive(f):
                    return f
        # 2) fallback: frame nào có window.connection hoặc .chipBalance
        for f in frames:
            try:
                if f.is_detached():
                    continue
                ok = f.evaluate(
                    "() => !!(window.connection || document.querySelector('.chipBalance')"
                    " || typeof window.createTable === 'function')"
                )
                if ok:
                    return f
            except Exception:
                continue
        return None

    def find_frame(self, max_wait=FRAME_WAIT, verbose=True):
        """Dò game frame. Trả về frame hoặc None (không raise)."""
        deadline = time.time() + max_wait
        first = True
        while time.time() < deadline:
            if self.page.is_closed():
                raise SessionDead("page đã bị đóng")
            f = self._scan_frames()
            if f is not None:
                self.gf = f
                if verbose and not first:
                    log("  ✓ Tìm thấy game frame")
                return f
            first = False
            time.sleep(2)
        if verbose:
            log(f"  ⚠ Không thấy game frame sau {max_wait}s")
        return None

    def frame(self, quick=True):
        """Trả về frame còn sống (dò nhanh nếu frame cũ chết), None nếu không có."""
        if self._frame_alive(self.gf):
            return self.gf
        self.gf = None
        return self.find_frame(max_wait=12 if quick else FRAME_WAIT, verbose=False)

    # ---------- eval ----------
    def eval(self, js, arg=None, default=None, retries=1):
        """Chạy JS trong game frame. Tự dò lại frame khi frame chết. Không raise."""
        for attempt in range(retries + 1):
            if self.page.is_closed():
                raise SessionDead("page đã bị đóng")
            f = self.frame()
            if f is None:
                if attempt < retries:
                    time.sleep(2)
                    continue
                return default
            try:
                return f.evaluate(js, arg)
            except Exception as e:
                txt = str(e)
                if is_frame_error(txt):
                    self.gf = None
                    if attempt < retries:
                        time.sleep(2)
                        continue
                if attempt < retries:
                    time.sleep(1)
                    continue
                return default
        return default

    # ---------- ws ----------
    def ws_ok(self):
        return bool(self.eval(
            "() => !!(window.connection && connection.ws && connection.ws.readyState === 1)",
            default=False, retries=0))

    def wait_ws(self, max_wait=WS_WAIT):
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if self.ws_ok():
                return True
            time.sleep(3)
        return False

    # ---------- health ----------
    def check_page_health(self):
        """Phát hiện logout / checkpoint -> SessionDead (không reload vô ích)."""
        if self.page.is_closed():
            raise SessionDead("page đã bị đóng")
        try:
            url = self.page.url or ""
        except Exception:
            raise SessionDead("không đọc được page.url")
        low = url.lower()
        if "checkpoint" in low or "/login" in low or "login.php" in low:
            raise SessionDead(f"bị logout/checkpoint ({url[:80]})")
        try:
            if self.page.locator('input[name="pass"], input[placeholder="Email or phone"]').count() > 0:
                raise SessionDead("hiện form đăng nhập -> cookie chết")
        except SessionDead:
            raise
        except Exception:
            pass

    # ---------- recover ----------
    def recover(self, reason=""):
        """Khôi phục game frame + WS. Raise SessionDead khi vượt hạn mức."""
        self.check_time()
        self.check_page_health()

        if self.reloads >= MAX_RELOADS_PER_SESSION:
            raise SessionDead(f"vượt quá {MAX_RELOADS_PER_SESSION} lần reload trong session")
        if self.recover_fail >= MAX_RECOVER_FAIL:
            raise SessionDead(f"recover thất bại {self.recover_fail} lần liên tiếp")

        since = time.time() - self.last_reload_ts
        if self.last_reload_ts and since < RELOAD_COOLDOWN:
            w = RELOAD_COOLDOWN - since
            if w > self.time_left() - MIN_TIME_LEFT:
                raise SessionDead("không đủ thời gian cho cooldown reload")
            log(f"  ⏳ cooldown reload: chờ {w:.0f}s")
            time.sleep(w)

        self.reloads += 1
        self.last_reload_ts = time.time()
        hard = (self.reloads % 2 == 0)  # luân phiên reload / goto
        log(f"  🔄 RECOVER #{self.reloads}/{MAX_RELOADS_PER_SESSION} "
            f"({'goto' if hard else 'reload'}) — lý do: {reason}")

        self.gf = None
        try:
            if hard:
                self.page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
            else:
                self.page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            txt = str(e)
            log(f"  ⚠ navigate lỗi: {txt[:120]}")
            if "closed" in txt.lower() or "crash" in txt.lower():
                raise SessionDead("page crash/closed khi navigate")
            self.recover_fail += 1
            if self.recover_fail >= MAX_RECOVER_FAIL:
                raise SessionDead("navigate liên tục thất bại")
            return False

        try:
            self.page.wait_for_timeout(15000)
        except Exception:
            time.sleep(15)

        self.check_page_health()

        f = self.find_frame(max_wait=FRAME_WAIT, verbose=True)
        if f is None:
            self.recover_fail += 1
            log(f"  ❌ recover fail ({self.recover_fail}/{MAX_RECOVER_FAIL}): không thấy game frame")
            if self.recover_fail >= MAX_RECOVER_FAIL:
                raise SessionDead("không tìm được game frame sau nhiều lần thử")
            return False

        if not self.wait_ws(WS_WAIT):
            self.recover_fail += 1
            log(f"  ❌ recover fail ({self.recover_fail}/{MAX_RECOVER_FAIL}): WS không kết nối")
            if self.recover_fail >= MAX_RECOVER_FAIL:
                raise SessionDead("WS không kết nối sau nhiều lần reload")
            return False

        self.recover_fail = 0
        log("  ✅ RECOVER OK — frame + WS sẵn sàng")
        return True

    def ensure_ready(self, reason="check", attempts=2):
        """Đảm bảo có frame sống + WS connected. Raise SessionDead nếu bó tay."""
        if self.frame() is not None and self.ws_ok():
            return True
        for _ in range(attempts):
            if self.recover(reason):
                return True
        # recover() đã tăng bộ đếm; nếu tới đây mà chưa raise thì báo thất bại mềm
        return self.frame() is not None and self.ws_ok()

    # ---------- tiện ích ----------
    def close_dialogs(self):
        self.eval("() => { try { $('.msgBoxBackGround,.msgBox').remove(); } catch(e) {} }",
                  retries=0)

    def get_bal(self):
        return self.eval(
            "() => document.querySelector('.chipBalance')?.textContent.trim() || '?'",
            default="?", retries=0) or "?"

    def is_account_blocked(self):
        return bool(self.eval("""() => {
            const sel = '[class*="msgBox"], [class*="dialog"], [class*="Dialog"], [class*="alert"]';
            const dialogs = document.querySelectorAll(sel);
            for (const d of dialogs) {
                if (d.offsetParent === null) continue;
                const txt = (d.textContent || '').toLowerCase();
                if (txt.includes('blocked') || txt.includes('khóa') || txt.includes('cấm')) return true;
            }
            return false;
        }""", default=False, retries=0))


# ============================================================
# JS payloads
# ============================================================
JS_TRANSFER = """(destId) => new Promise((resolve) => {
    try {
        const balEl = document.querySelector('.chipBalance');
        const balText = balEl ? balEl.textContent.trim() : '0';
        let balance = 0;
        const cleaned = balText.replace(/[^0-9kK.]/g, '');
        if (cleaned.toLowerCase().endsWith('k')) {
            balance = Math.round(parseFloat(cleaned.slice(0, -1)) * 1000);
        } else if (cleaned) {
            balance = parseInt(cleaned) || 0;
        }
        if (balance < 200) { resolve({success:false, error:'balance < 200', balance:balance}); return; }
        if (!window.connection || !connection.ws || connection.ws.readyState !== 1) {
            resolve({success:false, error:'ws not connected', balance:balance}); return;
        }
        const msg = new OutboundMessage("TRANSFER");
        msg.writeLong(destId);
        msg.writeLong(balance);
        let resolved = false;
        connection.send(msg, function(resp, ok) {
            if (resolved) return; resolved = true;
            try {
                const status = resp.readSignedByte();
                const txt = resp.readUtf16String ? resp.readUtf16String() : '';
                resolve({success:ok, status:status, message:txt, balance:balance, dest:destId});
            } catch(e) {
                resolve({success:ok, error:e.toString(), balance:balance});
            }
        });
        setTimeout(() => { if (!resolved) { resolved = true;
            resolve({success:false, error:'timeout', balance:balance}); } }, 12000);
    } catch(e) { resolve({success:false, error:e.toString()}); }
})"""

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
                try {
                    const amount = response.readLong();
                    if (window.Ads && window.Ads.RewardedVideo) {
                        window.Ads.RewardedVideo.videoIndex++;
                        if (window.Ads.RewardedVideo.updateRewardButton)
                            window.Ads.RewardedVideo.updateRewardButton();
                    }
                    resolve({success:true, amount:amount});
                } catch(e) { resolve({success:true, amount:0, error:e.toString()}); }
            } else {
                resolve({success:false, error:'no response'});
            }
        });
        setTimeout(() => { if (!resolved) { resolved = true;
            resolve({success:false, error:'timeout'}); } }, timeoutMs);
    } catch(e) { resolve({success:false, error:e.toString()}); }
})"""

JS_CLICK_WATCH = """() => {
    const dialogs = document.querySelectorAll('[class*="msgBox"]');
    for (const d of dialogs) {
        if (d.offsetParent !== null && (d.textContent || '').includes('enough coin')) {
            const buttons = d.querySelectorAll('input[type="button"], button');
            for (const b of buttons) {
                const val = (b.value || b.textContent || '').toLowerCase();
                if (val.includes('watch') || val.includes('video')) { b.click(); return true; }
            }
        }
    }
    return false;
}"""


# ============================================================
# ACTIONS
# ============================================================
def transfer_all_xu(s: GameSession, dest_id=TRANSFER_DEST_ID):
    """Chuyển toàn bộ xu về dest_id. Raise SessionDead nếu session hỏng."""
    if not s.ensure_ready("transfer"):
        return {"success": False, "error": "session chưa sẵn sàng"}
    res = s.eval(JS_TRANSFER, dest_id, default=None, retries=1)
    if res is None:
        return {"success": False, "error": "evaluate failed"}
    return res


def trigger_and_claim(s: GameSession):
    """Tạo bàn -> bắt alert thiếu xu -> gửi VIDEO_REWARD. Raise SessionDead nếu session hỏng."""
    if not s.ensure_ready("claim"):
        return {"success": False, "error": "session chưa sẵn sàng"}

    s.eval("() => { try { createTable(); } catch(e) {} }", retries=0)
    time.sleep(2)

    s.eval("""() => {
        const r = document.getElementById('radio_11');
        if (r) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles:true})); }
    }""", retries=0)
    time.sleep(0.5)

    s.eval("""() => {
        const b = document.querySelector('input[name="CREATE"]');
        if (b) b.click();
    }""", retries=0)
    time.sleep(3)

    alert_clicked = bool(s.eval(JS_CLICK_WATCH, default=False, retries=0))
    if alert_clicked:
        time.sleep(2)

    result = None
    max_attempts = 2
    for attempt in range(max_attempts):
        result = s.eval(JS_CLAIM, 15000, default={"success": False, "error": "evaluate failed"}, retries=0)
        if not isinstance(result, dict):
            result = {"success": False, "error": "bad result"}

        if result.get("success") and result.get("amount", 0) > 0:
            break

        err = result.get("error", "unknown")
        if attempt < max_attempts - 1:
            log(f"    attempt {attempt+1}/{max_attempts}: FAIL ({err}) — thử lại")
            s.close_dialogs()
            time.sleep(2)
            # chỉ recover khi thực sự mất WS/frame (tránh reload thừa)
            if not (s.frame() is not None and s.ws_ok()):
                if not s.ensure_ready("claim-retry", attempts=1):
                    log("    WS vẫn chết — bỏ retry")
                    break
            s.eval(JS_CLICK_WATCH, default=False, retries=0)
            time.sleep(1)
        else:
            log(f"    attempt {attempt+1}/{max_attempts}: FAIL ({err}) — bỏ qua")

    if not result.get("success") or result.get("amount", 0) == 0:
        result["method"] = "alert_clicked" if alert_clicked else "no_alert"
    return result


# ============================================================
# SESSION
# ============================================================
def run_session(p, fb_cookies, session_id, started_at):
    """Mở browser -> login -> load game -> loop (claim batch -> transfer).
    Trả về (reward, transferred, ok, fail, cookie_ok)."""
    log(f"\n########## SESSION {session_id} ##########")

    browser = None
    total_reward = 0
    total_transferred = 0
    ok = 0
    fail_total = 0
    cookie_ok = True

    try:
        # ===== [ANTI-BAN] Lấy fingerprint cố định cho cookie này =====
        fp = al.get_or_create_fingerprint(SINGLE_COOKIE_FILE) if al else None
        log(f"[ANTI-BAN] Fingerprint: UA={fp['user_agent'][:60]}... viewport={fp['viewport']} locale={fp['locale'] if fp else 'en-US'}" if fp else "[ANTI-BAN] Module anti_lock không khả dụng — chạy mode cũ")

        browser = p.chromium.launch(
            headless=HEADLESS,
            args=al.stealth_launch_args() if al else ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
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
        # Xóa None values để Playwright không phàn nàn
        context_kwargs = {k: v for k, v in context_kwargs.items() if v is not None}
        context = browser.new_context(**context_kwargs)

        # ===== [ANTI-BAN] Inject stealth JS vào mọi frame trước khi load =====
        if al and fp:
            try:
                context.add_init_script(al.build_stealth_js(fp))
                log("[ANTI-BAN] Stealth JS injected OK")
            except Exception as e:
                log(f"[ANTI-BAN] Stealth JS inject fail: {e}")

        try:
            cookies = []
            for c in fb_cookies:
                c = dict(c)
                c["domain"] = ".facebook.com"
                cookies.append(c)
            context.add_cookies(cookies)
        except Exception as e:
            log(f"  ERROR add_cookies: {e}")
            return 0, 0, 0, 0, False

        page = context.new_page()
        page.set_default_timeout(45000)
        s = GameSession(page, started_at)

        # ===== [1] Login FB =====
        log("[1] Login FB...")
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log(f"  ERROR goto FB: {e}")
            return 0, 0, 0, 0, True
        page.wait_for_timeout(5000)
        try:
            s.check_page_health()
        except SessionDead as e:
            log(f"  ❌ {e}")
            return 0, 0, 0, 0, False
        log("  OK")

        # ===== [ANTI-BAN] Warm-up account: scroll FB feed 5-15s trước khi vào game =====
        if WARMUP_ENABLED and al:
            log("[ANTI-BAN] Warm-up: scroll FB feed...")
            try:
                al.warm_up_account(page, max_s=15)
                log("  Warm-up OK")
            except Exception as e:
                log(f"  Warm-up fail (ignore): {e}")

        # ===== [2] Mở game =====
        log("[2] Open game...")
        try:
            page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"  ERROR goto game: {e}")
            return 0, 0, 0, 0, True
        page.wait_for_timeout(20000)

        if s.find_frame(max_wait=FRAME_WAIT) is None:
            log("  ⚠ Chưa thấy game frame — thử recover 1 lần")
            try:
                s.recover("frame không xuất hiện lúc khởi động")
            except SessionDead as e:
                log(f"  ❌ SESSION DEAD: {e}")
                return 0, 0, 0, 0, True
        log("  Game loaded")

        # ===== [3] Chờ WS =====
        log("[3] Wait WS...")
        if not s.wait_ws(WS_WAIT):
            log("  ⚠ WS chưa connect — recover")
            try:
                if not s.ensure_ready("ws khởi động"):
                    log("  ❌ Không kết nối được WS")
                    return 0, 0, 0, 0, True
            except SessionDead as e:
                log(f"  ❌ SESSION DEAD: {e}")
                return 0, 0, 0, 0, True
        log("  WS connected")

        # ===== Account blocked? =====
        if s.is_account_blocked():
            log("  ❌ ACCOUNT BLOCKED — dừng")
            s.eval("""() => {
                const btns = document.querySelectorAll('input[type="button"], button');
                for (const b of btns) {
                    const t = (b.value || b.textContent || '').toLowerCase().trim();
                    if (t === 'ok' || t === 'đóng' || t === 'close') b.click();
                }
            }""", retries=0)
            return 0, 0, 0, 0, False

        bal_start = s.get_bal()
        log(f"  Balance: {bal_start}")

        # ===== [ANTI-BAN] Daily quota check =====
        if al:
            ok_quota, reason = al.check_daily_quota(
                SINGLE_COOKIE_FILE,
                max_claims=MAX_CLAIMS_PER_DAY,
                max_transfers=MAX_TRANSFERS_PER_DAY,
                max_amount=MAX_TRANSFER_AMOUNT_PER_DAY,
            )
            if not ok_quota:
                log(f"  ⏸ [ANTI-BAN] Bỏ qua session: {reason}")
                return 0, 0, 0, 0, True
            al.record_session_start(SINGLE_COOKIE_FILE)

        # ===== [4] BATCH LOOP =====
        log(f"\n[4] BATCH MODE: claim ~{CLAIM_BATCH} lần/jitter -> transfer -> lặp lại")
        claim_count = 0
        batch_no = 0
        consec_fail = 0
        # dest_id cố định theo env TRANSFER_DEST_ID (set trong .github/workflows/ckN.yml)
        # KHÔNG xoay vòng — mỗi cookie có 1 hub account riêng theo yml.
        dest_id = TRANSFER_DEST_ID

        while True:
            if s.time_left() <= MIN_TIME_LEFT:
                log(f"  ⏹ Hết thời gian MAX_RUNTIME, dừng session.")
                break

            batch_no += 1

            # [ANTI-BAN] Batch size jitter (25-55 thay vì fixed 40)
            cur_batch = al.random_batch_size(CLAIM_BATCH, lo=max(10, CLAIM_BATCH-5), hi=CLAIM_BATCH+5) if al else CLAIM_BATCH

            # --- pre-batch transfer nếu balance cao ---
            bal_before_batch = s.get_bal()
            bal_num = parse_balance_num(bal_before_batch)
            if TRANSFER_ENABLED and bal_num > PRE_CLAIM_TRANSFER_THRESHOLD:
                log(f"\n[Pre-batch] Balance {bal_num:,} > {PRE_CLAIM_TRANSFER_THRESHOLD:,} -> transfer trước")
                r = transfer_all_xu(s, dest_id)
                if r.get("success"):
                    amt = r.get("balance", 0)
                    total_transferred += amt
                    log(f"  ✅ Pre-batch transfer {amt:,} xu -> {dest_id}")
                    msg = r.get("message", "")
                    if msg:
                        log(f"     Server: {msg[:80]}")
                    # [ANTI-BAN] Soft-ban detect + record transfer
                    if al:
                        if al.detect_soft_ban(msg) or al.detect_soft_ban(r.get("error", "")):
                            log("  🚫 [ANTI-BAN] Soft-ban detected — back-off 15 phút")
                            al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                            return total_reward, total_transferred, ok, fail_total, True
                        al.record_transfer(SINGLE_COOKIE_FILE, amt)
                    time.sleep(2)
                else:
                    log(f"  ❌ Pre-batch transfer fail: {r.get('error', 'unknown')}")

            # --- claim batch ---
            log(f"\n[Batch #{batch_no}] bắt đầu {cur_batch} claim (dest={dest_id})...")
            stop_session = False

            for i in range(cur_batch):
                if s.time_left() <= MIN_TIME_LEFT:
                    log("  ⏹ Hết thời gian trong lúc claim, dừng.")
                    stop_session = True
                    break

                bal_before = s.get_bal()
                log(f"  [Claim #{claim_count + 1}] Balance: {bal_before} -> claiming...")
                s.close_dialogs()
                al.jitter_sleep(1.0, 1.0) if al else time.sleep(1)

                result = trigger_and_claim(s)

                # [ANTI-BAN] Soft-ban detect trong response
                if al and (al.detect_soft_ban(result.get("error", "")) or
                           al.detect_soft_ban(result.get("message", ""))):
                    log("  🚫 [ANTI-BAN] Soft-ban detected trong claim response — back-off 15 phút")
                    al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                    return total_reward, total_transferred, ok, fail_total, True

                if result.get("success") and result.get("amount", 0) > 0:
                    amount = result["amount"]
                    total_reward += amount
                    ok += 1
                    claim_count += 1
                    consec_fail = 0
                    if al:
                        al.record_claim(SINGLE_COOKIE_FILE, amount)
                    # [ANTI-BAN] Human-like delay (Poisson 2-6s) thay vì fixed
                    al.jitter_sleep(1.0, 0.8) if al else time.sleep(1)
                    bal_after = s.get_bal()
                    log(f"    ✅ OK +{amount} | {bal_before} -> {bal_after} | tổng reward={total_reward:,}")

                    # [ANTI-BAN] Idle break mỗi IDLE_EVERY_CLAIMS claim — giả lập người nghỉ
                    if al and IDLE_EVERY_CLAIMS > 0:
                        try:
                            al.maybe_idle_browse(page, claim_count, every=IDLE_EVERY_CLAIMS)
                        except Exception:
                            pass
                else:
                    fail_total += 1
                    consec_fail += 1
                    err = result.get("error", "unknown")
                    method = result.get("method", "")
                    log(f"    ❌ FAIL ({err}) [{method}] | fail liên tiếp: {consec_fail}")

                    # [GAME-RULE] Phát hiện fail vì balance cao (>50k — game chặn claim khi >55k)
                    # → ép transfer ngay, không recover vô ích (recover cũng không giúp gì được)
                    cur_bal_num = parse_balance_num(s.get_bal())
                    if cur_bal_num >= 50000:
                        log(f"  💸 Balance {cur_bal_num:,} >= 50k — game chặn claim, ÉP TRANSFER ngay")
                        # thoát vòng for để chạy post-batch transfer
                        break

                    if consec_fail >= MAX_CONSEC_CLAIM_FAIL:
                        log(f"  ⚠ {consec_fail} lần fail liên tiếp -> thử recover 1 lần")
                        try:
                            if s.ensure_ready("quá nhiều claim fail", attempts=1):
                                consec_fail = 0
                            else:
                                log("  ❌ recover không ăn thua -> kết thúc session")
                                stop_session = True
                                break
                        except SessionDead:
                            raise
                        # nếu recover OK thì tiếp tục batch

                # [ANTI-BAN] Human-like delay giữa các claim (Poisson) thay vì fixed 3s+rand(0,1.5)
                if al:
                    al.human_delay(max(2.0, DELAY), max(4.0, DELAY + 3.0))
                else:
                    time.sleep(DELAY + random.uniform(0, 1.5))

            # --- transfer sau batch ---
            bal_after_batch = s.get_bal()
            bal_after_num = parse_balance_num(bal_after_batch)
            if TRANSFER_ENABLED and bal_after_num > 200:
                log(f"\n[Transfer] Balance: {bal_after_batch} -> đang chuyển về {dest_id}...")
                r = transfer_all_xu(s, dest_id)
                if r.get("success"):
                    amt = r.get("balance", 0)
                    total_transferred += amt
                    log(f"  ✅ Đã chuyển {amt:,} xu -> {dest_id}")
                    msg = r.get("message", "")
                    if msg:
                        log(f"     Server: {msg[:80]}")
                    # [ANTI-BAN] Record transfer + soft-ban detect
                    if al:
                        if al.detect_soft_ban(msg) or al.detect_soft_ban(r.get("error", "")):
                            log("  🚫 [ANTI-BAN] Soft-ban detected — back-off 15 phút")
                            al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                            return total_reward, total_transferred, ok, fail_total, True
                        al.record_transfer(SINGLE_COOKIE_FILE, amt)
                    time.sleep(2)
                    log(f"     Balance sau transfer: {s.get_bal()}")
                else:
                    log(f"  ❌ Transfer FAIL: {r.get('error', 'unknown')}")
                    msg = r.get("message", "")
                    if msg:
                        log(f"     Server: {msg[:80]}")
                    # [ANTI-BAN] Soft-ban detect trong fail response
                    if al and al.detect_soft_ban(r.get("error", "")):
                        log("  🚫 [ANTI-BAN] Soft-ban detected trong transfer fail — back-off 15 phút")
                        al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                        return total_reward, total_transferred, ok, fail_total, True
            else:
                log(f"  ⚠ Balance {bal_after_num:,} ≤ 200, bỏ qua transfer")

            if stop_session:
                break
            # [ANTI-BAN] Rest giữa 2 batch cũng nên có jitter
            if al:
                al.human_delay(max(3.0, DELAY), max(8.0, DELAY + 5.0))
            else:
                time.sleep(DELAY)

    except SessionDead as e:
        log(f"\n[SESSION {session_id}] 💀 SESSION DEAD: {e}")
        if "cookie chết" in str(e) or "logout" in str(e) or "checkpoint" in str(e):
            cookie_ok = False
    except Exception as e:
        log(f"\n[SESSION {session_id}] ⚠ Lỗi không mong đợi: {type(e).__name__}: {str(e)[:160]}")
    finally:
        try:
            if browser:
                browser.close()
                log("  🧹 Đã đóng browser")
        except Exception:
            pass

    log(f"[SESSION {session_id}] Kết thúc | OK={ok} fail={fail_total} "
        f"| reward={total_reward:,} | transferred={total_transferred:,}")
    return total_reward, total_transferred, ok, fail_total, cookie_ok


# ============================================================
# MAIN
# ============================================================
def main():
    log("=" * 64)
    log("FB Tien Len Mien Nam reward bot v10 — BATCH + ROBUST RECOVERY")
    log("=" * 64)
    log(f"Config: CLAIM_BATCH={CLAIM_BATCH} COOLDOWN={DELAY}s REST={REST}s "
        f"MAX_RUNTIME={MAX_RUNTIME}s TRANSFER_DEST={TRANSFER_DEST_ID}")
    log(f"Recovery: MAX_SESSIONS={MAX_SESSIONS} MAX_RELOADS={MAX_RELOADS_PER_SESSION} "
        f"RELOAD_COOLDOWN={RELOAD_COOLDOWN}s MAX_RECOVER_FAIL={MAX_RECOVER_FAIL}")
    log("=" * 64)

    cookie_entries = load_single_cookie_set(SINGLE_COOKIE_FILE)
    if not cookie_entries:
        log(f"[STOP] Không nạp được cookie từ {SINGLE_COOKIE_FILE}")
        return 1

    entry = cookie_entries[0]
    fb_cookies = parse_cookie(entry["raw"])
    if not fb_cookies:
        log(f"[STOP] Cookie {entry['file']} parse rỗng — có thể sai định dạng.")
        return 1
    log(f"[COOKIE] ✅ Đã parse {len(fb_cookies)} cookies từ {entry['file']}")

    # [ANTI-BAN] Daily quota check ngay từ đầu
    if al:
        ok_quota, reason = al.check_daily_quota(
            SINGLE_COOKIE_FILE,
            max_claims=MAX_CLAIMS_PER_DAY,
            max_transfers=MAX_TRANSFERS_PER_DAY,
            max_amount=MAX_TRANSFER_AMOUNT_PER_DAY,
        )
        if not ok_quota:
            log(f"\n[ANTI-BAN] ⏸ Dừng ngay: {reason}")
            return 0

    # [ANTI-BAN] Random session start offset để 8 cookie không đồng loạt bật cùng lúc
    if al and SESSION_START_JITTER > 0:
        wait = al.random_session_offset(SESSION_START_JITTER)
        log(f"\n[ANTI-BAN] ⏳ Session start jitter: nghỉ {wait:.1f}s trước khi mở browser...")

    started_at = time.time()
    session_id = 0
    grand_reward = 0
    grand_transfer = 0
    grand_ok = 0
    grand_fail = 0

    with sync_playwright() as p:
        while session_id < MAX_SESSIONS:
            left = MAX_RUNTIME - (time.time() - started_at)
            if left <= MIN_TIME_LEFT:
                log(f"\n[STOP] Hết thời gian tổng ({MAX_RUNTIME}s).")
                break

            session_id += 1
            log(f"\n{'=' * 64}")
            log(f"[Session {session_id}/{MAX_SESSIONS}] {time.strftime('%H:%M:%S')} "
                f"| còn {int(left)}s")
            log("=" * 64)

            reward, transferred, ok, fail, cookie_ok = run_session(
                p, fb_cookies, session_id, started_at)

            grand_reward += reward
            grand_transfer += transferred
            grand_ok += ok
            grand_fail += fail

            log(f"\n[Tổng tạm] claims OK={grand_ok} fail={grand_fail} "
                f"| reward={grand_reward:,} | transferred={grand_transfer:,}")

            if not cookie_ok:
                log("[STOP] Cookie hỏng / account bị khóa — không mở session mới.")
                break

            left = MAX_RUNTIME - (time.time() - started_at)
            if left <= MIN_TIME_LEFT:
                break
            if session_id < MAX_SESSIONS:
                wait = min(REST, max(5, int(left - MIN_TIME_LEFT)))
                log(f"\n💤 Nghỉ {wait}s rồi mở session mới...")
                time.sleep(wait)

    log(f"\n{'=' * 64}")
    log(f"TỔNG KẾT: {session_id} session | {grand_ok} claims OK | {grand_fail} fail "
        f"| reward={grand_reward:,} | transferred={grand_transfer:,}")
    log("=" * 64)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n[EXIT] Người dùng dừng bot.")
        sys.exit(130)
