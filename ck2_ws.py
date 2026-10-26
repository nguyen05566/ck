#!/usr/bin/env python3
"""
FB phom_tala reward bot — WebSocket target finding + SURRENDER
Base: ck2.py (BATCH TRANSFER + ROBUST RECOVERY)

Changes:
  1. Target ID -> 68952059, target name -> nguyen123456
  2. Bỏ page refresh (reload/goto). Dùng place.enter() để navigate.
  3. Tìm target qua WebSocket (PLAYER_ENTERED 0x019E) thay vì DOM parse.
  4. Thêm SURRENDER test qua OutboundMessage("SURRENDER").
  5. WebSocket endpoint: wss://www.gamevh.me/ws/gameServer
     Handlers: ENTER_PLACE, PLAYER_ENTERED, TABLE_IN_ROOM_CHANGED,
               RELOAD, CONFIG, NAVIGATE, CONFIRM_NAVIGATE, BALANCE_CHANGED
     Commands: "VIDEO_REWARD", "SURRENDER", "PING"
"""

import os, sys, time, random, struct

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import board_dom_merged as m
except Exception:
    m = None

try:
    import anti_lock as al
except Exception as e:
    print(f"[WARN] Không nạp được anti_lock.py: {e}", flush=True)
    al = None

from playwright.sync_api import sync_playwright

try:
    import ws_bridge
except Exception as _e:
    print(f"[WARN] Không nạp được ws_bridge.py: {_e}", flush=True)
    ws_bridge = None

# ============================================================
# CONFIG
# ============================================================
GAME_URL = "https://www.facebook.com/gaming/play/gomoku-vh"
WS_URL = "wss://www.gamevh.me/ws/gameServer"

CLAIM_BATCH   = int(os.environ.get("CLAIM_BATCH", "40"))
DELAY         = float(os.environ.get("COOLDOWN", "3"))
REST          = int(os.environ.get("REST_BETWEEN_RUNS", "30"))
MAX_RUNTIME   = int(os.environ.get("MAX_RUNTIME", str(330 * 60)))
HEADLESS      = os.environ.get("HEADLESS", "true").lower() == "true"

TRANSFER_DEST_ID = 68952059
SINGLE_COOKIE_FILE = os.environ.get("SINGLE_COOKIE_FILE", "ck2.txt").strip()
TARGET_NAME = os.environ.get("TARGET_NAME", "nguyen123456")
TARGET_ID   = int(os.environ.get("TARGET_ID", "68952059"))

MAX_SESSIONS            = int(os.environ.get("MAX_SESSIONS", "12"))
MAX_RELOADS_PER_SESSION = int(os.environ.get("MAX_RELOADS", "8"))
MAX_RECOVER_FAIL        = int(os.environ.get("MAX_RECOVER_FAIL", "3"))
RELOAD_COOLDOWN         = int(os.environ.get("RELOAD_COOLDOWN", "45"))
FRAME_WAIT              = int(os.environ.get("FRAME_WAIT", "90"))
WS_WAIT                 = int(os.environ.get("WS_WAIT", "60"))
MAX_CONSEC_CLAIM_FAIL   = int(os.environ.get("MAX_CONSEC_CLAIM_FAIL", "6"))
MIN_TIME_LEFT           = 90

MAX_CLAIMS_PER_DAY         = int(os.environ.get("MAX_CLAIMS_PER_DAY", str(getattr(al, "DEFAULT_MAX_CLAIMS_PER_DAY", 0) if al else 0)))
MAX_TRANSFERS_PER_DAY      = int(os.environ.get("MAX_TRANSFERS_PER_DAY", str(getattr(al, "DEFAULT_MAX_TRANSFERS_PER_DAY", 0) if al else 0)))
MAX_TRANSFER_AMOUNT_PER_DAY = int(os.environ.get("MAX_TRANSFER_AMOUNT_PER_DAY", str(getattr(al, "DEFAULT_MAX_TRANSFER_AMOUNT", 2_000_000) if al else 2_000_000)))

SESSION_START_JITTER = int(os.environ.get("SESSION_START_JITTER", "180"))
IDLE_EVERY_CLAIMS = int(os.environ.get("IDLE_EVERY_CLAIMS", "60"))
WARMUP_ENABLED = os.environ.get("WARMUP_ENABLED", "true").lower() == "true"

PRE_CLAIM_TRANSFER_THRESHOLD = int(os.environ.get("PRE_CLAIM_TRANSFER_THRESHOLD", "50000"))
BET_MAX = int(os.environ.get("BET_MAX", "100000"))
CLAIM_MIN = int(os.environ.get("CLAIM_MIN", "40000"))
CLAIM_MAX = int(os.environ.get("CLAIM_MAX", "55000"))
SURRENDER_BET_MAX = int(os.environ.get("SURRENDER_BET_MAX", "100000"))
TARGET_WAIT_SECONDS = int(os.environ.get("TARGET_WAIT_SECONDS", "60"))
MAX_TABLES_TO_SCAN = int(os.environ.get("MAX_TABLES_TO_SCAN", "30"))
TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"

FRAME_ERR_HINTS = (
    "detach", "execution context", "target closed", "has been closed",
    "frame was", "navigation", "page closed", "browser has been closed",
    "connection closed",
)
WS_IN_CMD_HANDLERS = (
    "ENTER_PLACE", "PLAYER_ENTERED", "TABLE_IN_ROOM_CHANGED",
    "RELOAD", "CONFIG", "NAVIGATE", "CONFIRM_NAVIGATE", "BALANCE_CHANGED",
)
WS_OUT_CMDS = ("VIDEO_REWARD", "SURRENDER", "PING")

PLAYER_ENTERED_CMD_ID = 0x019E


def log(msg=""):
    print(msg, flush=True)


def is_frame_error(err_text: str) -> bool:
    t = (err_text or "").lower()
    return any(h in t for h in FRAME_ERR_HINTS)


class SessionDead(Exception):
    pass


# ============================================================
# WS-TAP JS (install interceptor on WebSocket to capture inbound messages)
# ============================================================
JS_INSTALL_WS_TAP = """() => {
    if (window.__tap_installed) return true;
    window.__tap = [];
    const conn = window.connection;
    if (!conn || !conn.ws) return false;
    const ws = conn.ws;
    const orig = ws.onmessage;
    ws.binaryType = 'arraybuffer';
    ws.onmessage = function(ev) {
        let bytes = null;
        try {
            if (ev.data instanceof ArrayBuffer) bytes = Array.from(new Uint8Array(ev.data));
            else if (ev.data && ev.data.arrayBuffer) bytes = Array.from(new Uint8Array(ev.data.arrayBuffer ? ev.data.arrayBuffer() : ev.data));
        } catch(e) {}
        if (bytes) window.__tap.push({ts: Date.now(), bytes, len: bytes.length});
        if (orig) return orig.call(ws, ev);
    };
    window.__tap_installed = true;
    return true;
}"""

JS_CLEAR_TAP = "() => { window.__tap = []; window.__last_tap_count = 0; return true; }"
JS_GET_TAP = "() => window.__tap || []"

# Scan all visible tables (id, bet, player) from DOM
JS_SCAN_ALL_TABLES = """() => {
    const out = [];
    document.querySelectorAll('div[id^=table_]').forEach(t => {
        if (t.offsetParent === null) return;
        const r = t.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return;
        const text = (t.innerText || '').trim();
        const parts = text.split('\\n');
        const id = parseInt(t.id.replace('table_', ''));
        if (isNaN(id)) return;
        const cls = (t.className || '').toString();
        out.push({id, bet: parts[0] || '', player: parts[1] || '', cls, has_exceed: cls.includes('exceed')});
    });
    return out;
}"""

JS_CLICK_TABLE = """(tableId) => {
    const div = document.getElementById('table_' + tableId);
    if (div && div.offsetParent !== null) { div.click(); return {clicked: true}; }
    return {clicked: false};
}"""

JS_CLOSE_TABLE_POPUP = """() => {
    const closeBtn = document.querySelector('.msgBox .closeButton');
    if (closeBtn) { closeBtn.click(); return true; }
    return false;
}"""

JS_CLOSE_POPUPS = """() => {
    for (const b of document.querySelectorAll('.popup_button.confirm-button, button')) {
        const t = (b.innerText || '').trim();
        if (/^OK$|^Tôi đã hiểu$|^Đồng ý$/i.test(t) && b.offsetParent !== null) { b.click(); return true; }
    }
    for (const b of document.querySelectorAll('a, button')) {
        if (b.id === 'closeLuckyWheelButton' && b.offsetParent !== null) { b.click(); return true; }
    }
    return false;
}"""

JS_GET_STATE = """() => ({
    path: window.place ? (window.place.thisPath || place.thisPath) : null,
    balance: document.querySelector('.chipBalance')?.textContent || null,
    playerId: window.currentPlayerId || null,
})"""

JS_GO_LOBBY = """() => {
    try {
        if (typeof place.enterParent === 'function') { place.enterParent(); return 'enterParent'; }
        if (typeof place.leaveTable === 'function') { place.leaveTable(); return 'leaveTable'; }
        if (typeof place.enter === 'function') { place.enter('Lobby'); return 'enter: Lobby'; }
        return 'no leave/enter function';
    } catch(e) { return 'err: ' + e.toString(); }
}"""

# OutboundMessage approach (runs inside browser, uses game's connection.send)
JS_SEND_SURRENDER = """() => new Promise((resolve) => {
    try {
        if (!window.connection || !connection.ws || connection.ws.readyState !== 1) {
            resolve({ok: false, error: 'ws not connected'}); return;
        }
        const msg = new OutboundMessage("SURRENDER");
        let done = false;
        connection.send(msg, function(resp, ok) {
            if (done) return; done = true;
            const out = {ok, command: resp.command};
            try {
                const d = resp.data;
                if (d && d.length >= 2) {
                    out.err_code = d[1];
                    if (d.length > 5) {
                        try {
                            let s = new TextDecoder('utf-16be', {fatal: false}).decode(d.slice(5));
                            s = s.replace(/\\u0000/g, ' | ').replace(/[\\u0000-\\u001F]+$/g, '');
                            out.err_msg = s.slice(0, 200);
                        } catch(e) {}
                    }
                }
            } catch(e) {}
            resolve(out);
        });
        setTimeout(() => { if (!done) { done = true; resolve({ok: false, error: 'timeout 8s'}); } }, 8000);
    } catch(e) { resolve({ok: false, error: e.toString()}); }
})"""

# PING — keepalive via OutboundMessage
JS_SEND_PING = """() => new Promise((resolve) => {
    try {
        if (!window.connection || !connection.ws || connection.ws.readyState !== 1) {
            resolve({ok: false, error: 'ws not connected'}); return;
        }
        const msg = new OutboundMessage("PING");
        let done = false;
        connection.send(msg, function(resp, ok) {
            if (done) return; done = true;
            resolve({ok, command: resp.command});
        });
        setTimeout(() => { if (!done) { done = true; resolve({ok: false, error: 'timeout'}); } }, 5000);
    } catch(e) { resolve({ok: false, error: e.toString()}); }
})"""

# Claim (VIDEO_REWARD) — same as ck2.py / auto_cycle.py
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

JS_CREATE_TABLE = """() => { try { createTable(); } catch(e) {} }"""
JS_SELECT_RADIO_11 = """() => {
    const r = document.getElementById('radio_11');
    if (r) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles:true})); }
}"""
JS_CLICK_CREATE = """() => {
    const b = document.querySelector('input[name="CREATE"]');
    if (b) b.click();
}"""
JS_CLICK_WATCH = """() => {
    const dialogs = document.querySelectorAll('[class*=msgBox]');
    for (const d of dialogs) {
        if (d.offsetParent === null) continue;
        if ((d.textContent || '').includes('enough coin')) {
            const buttons = d.querySelectorAll('input[type="button"], button');
            for (const b of buttons) {
                const val = (b.value || b.textContent || '').toLowerCase();
                if (val.includes('watch') || val.includes('video')) { b.click(); return true; }
            }
        }
    }
    return false;
}"""
JS_CLICK_VAO_CHOI = """() => {
    for (const b of document.querySelectorAll('input.msgButton, button, input[type=button]')) {
        const t = (b.value || b.innerText || '').trim();
        if (t === 'Vào chơi' && b.offsetParent !== null) { b.click(); return {clicked: true}; }
    }
    return {clicked: false};
}"""
JS_FIND_BAT_DAU = """() => {
    for (const b of document.querySelectorAll('button, input[type=button], [role=button], a')) {
        const r = b.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const t = (b.innerText || b.textContent || b.value || '').trim();
        if (t.includes('Bắt đầu')) return {found: true, text: t};
    }
    return {found: false};
}"""
JS_CLICK_BAT_DAU = """() => {
    for (const b of document.querySelectorAll('button, input[type=button], [role=button], a')) {
        const r = b.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const t = (b.innerText || b.textContent || b.value || '').trim();
        if (t.includes('Bắt đầu')) { b.click(); return {clicked: true, text: t}; }
    }
    return {clicked: false};
}"""
JS_CLICK_OK = """() => {
    for (const b of document.querySelectorAll('.popup_button.confirm-button, button, input[type=button], a')) {
        if (b.offsetParent === null) continue;
        const r = b.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const t = (b.innerText || b.textContent || b.value || '').trim();
        if (/^(OK|Đồng ý|Tiếp tục|Đóng|Chơi lại|Về lobby|Về phòng chờ)$/i.test(t)) {
            b.click(); return {clicked: true, text: t};
        }
    }
    return {clicked: false};
}"""
JS_CLOSE_DIALOGS = """() => {
    try { document.querySelectorAll('.msgBoxBackGround,.msgBox').forEach(el => el.remove()); return true; }
    catch(e) { return false; }
}"""


# ============================================================
# UTILITIES
# ============================================================

def parse_bet(s):
    if not s or s == "?" or (isinstance(s, str) and s.strip() == ""):
        return 0
    s = str(s).strip().lower().replace(",", "").replace(" ", "")
    s = "".join(c for c in s if c.isdigit() or c in ".km")
    try:
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1000000)
        return int(float(s))
    except Exception:
        return 0


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
# GAME SESSION — bỏ page refresh, dùng place.enter() để navigate
# ============================================================
class GameSession:
    def __init__(self, page, started_at):
        self.page = page
        self.started_at = started_at
        self.gf = None
        self.recovers = 0
        self.last_recover_ts = 0.0

    def time_left(self):
        return MAX_RUNTIME - (time.time() - self.started_at)

    def check_time(self):
        if self.time_left() <= MIN_TIME_LEFT:
            raise SessionDead("hết thời gian MAX_RUNTIME")

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
        if self._frame_alive(self.gf):
            return self.gf
        self.gf = None
        return self.find_frame(max_wait=12 if quick else FRAME_WAIT, verbose=False)

    def eval(self, js, arg=None, default=None, retries=1):
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

    def ws_target(self):
        try:
            f = self.frame()
        except Exception:
            f = None
        return f or self.page

    def ws_capture(self, limit=0):
        if not ws_bridge:
            return []
        return ws_bridge.capture_frames(self.ws_target(), limit=limit)

    def ws_send(self, payload, sock_index=0):
        if not ws_bridge:
            return {"ok": False, "error": "no ws_bridge"}
        return ws_bridge.send_raw(self.ws_target(), payload, sock_index)

    def check_page_health(self):
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

    # === RECOVER — bỏ page reload/goto, dùng place.enter() ===
    def recover(self, reason=""):
        """Khôi phục game frame + WS. KHÔNG reload trang — dùng place.enter()."""
        self.check_time()
        self.check_page_health()

        self.recovers += 1
        self.last_recover_ts = time.time()
        log(f"  🔄 RECOVER #{self.recovers} — lý do: {reason}")

        self.gf = None
        try:
            f = self.find_frame(max_wait=FRAME_WAIT, verbose=True)
            if f is None:
                self.recovers += 1
                log(f"  ❌ recover: không thấy game frame")
                if self.recovers >= 3:
                    raise SessionDead("không tìm được game frame sau nhiều lần thử")
                return False
        except SessionDead:
            raise

        if not self.wait_ws(WS_WAIT):
            self.recovers += 1
            log(f"  ❌ recover: WS không kết nối")
            if self.recovers >= 3:
                raise SessionDead("WS không kết nối sau nhiều lần thử")
            return False

        # Dùng place.enter() để navigate về Lobby thay vì reload trang
        log("  ⏳ place.enter('Lobby') để đồng bộ vị trí...")
        self.eval(JS_GO_LOBBY, retries=0)
        time.sleep(3)

        self.recovers = 0
        log("  ✅ RECOVER OK — frame + WS + place đã sẵn sàng")
        return True

    def ensure_ready(self, reason="check", attempts=2):
        if self.frame() is not None and self.ws_ok():
            return True
        for _ in range(attempts):
            if self.recover(reason):
                return True
        return self.frame() is not None and self.ws_ok()

    # === WS target finding (based on PLAYER_ENTERED handler) ===
    def install_ws_tap(self):
        return self.eval(JS_INSTALL_WS_TAP, default=False, retries=0)

    def scan_tables(self):
        return self.eval(JS_SCAN_ALL_TABLES, default=[], retries=0)

    def get_tap(self):
        return self.eval(JS_GET_TAP, default=[], retries=0)

    def clear_tap(self):
        return self.eval(JS_CLEAR_TAP, default=False, retries=0)

    def find_target_by_id(self, target_id, bet_max, max_tables_to_scan=MAX_TABLES_TO_SCAN):
        """Tìm bàn có target_id qua WebSocket PLAYER_ENTERED handler."""
        target_be = struct.pack('>I', int(target_id))
        target_bytes_list = list(target_be)
        self.install_ws_tap()
        tables = self.scan_tables()
        if not tables:
            return None
        log(f"  [scan-by-id] {len(tables)} bàn — kiếm target_id={target_id}")
        tables_to_scan = tables[:max_tables_to_scan]
        found = None
        for t in tables_to_scan:
            self.eval(JS_CLOSE_TABLE_POPUP, retries=0)
            time.sleep(0.1)
            self.clear_tap()
            self.eval(JS_CLICK_TABLE, t["id"], retries=0)
            time.sleep(0.4)
            tap = self.get_tap()
            for msg in tap:
                b = msg.get('bytes') or []
                for off in range(len(b) - 3):
                    if b[off:off+4] == target_bytes_list:
                        log(f"  ✓✓✓ FOUND target_id={target_id} at bàn {t['id']}")
                        found = t
                        break
                if found:
                    break
            if found:
                break
        self.eval(JS_CLOSE_TABLE_POPUP, retries=0)
        time.sleep(0.3)
        if not found:
            log(f"  [scan-by-id] target_id={target_id} không có trong {len(tables_to_scan)} bàn")
            return None
        bet_val = parse_bet(found.get("bet", "999999"))
        if bet_val > bet_max:
            log(f"  [scan-by-id] bet {bet_val:,} > bet_max {bet_max:,}")
            return None
        return found

    def get_bal(self):
        return self.eval(
            "() => document.querySelector('.chipBalance')?.textContent.trim() || '?'",
            default="?", retries=0) or "?"

    def get_balance(self):
        s = self.eval(JS_GET_STATE, default={}, retries=0)
        return parse_bet(s.get("balance")), s

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

    def close_dialogs(self):
        self.eval("() => { try { $('.msgBoxBackGround,.msgBox').remove(); } catch(e) {} }", retries=0)


# ============================================================
# ACTIONS
# ============================================================
def transfer_all_xu(s: GameSession, dest_id=TRANSFER_DEST_ID):
    if not s.ensure_ready("transfer"):
        return {"success": False, "error": "session chưa sẵn sàng"}
    res = s.eval("""(destId) => new Promise((resolve) => {
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
                let txt = '';
                try {
                    if (ok && resp && typeof resp.readString === 'function') txt = resp.readString();
                } catch(e) { txt = ''; }
                resolve({success: ok === true, status: ok === true ? 0 : -1, message:txt, balance:balance,
                         dest:destId, error: ok === true ? '' : (txt || 'transfer rejected')});
            });
            setTimeout(() => { if (!resolved) { resolved = true;
                resolve({success:false, error:'timeout', balance:balance}); } }, 12000);
        } catch(e) { resolve({success:false, error:e.toString()}); }
    })""", dest_id, default=None, retries=1)
    if res is None:
        return {"success": False, "error": "evaluate failed"}
    return res


def trigger_and_claim(s: GameSession):
    """Claim qua OutboundMessage('VIDEO_REWARD')."""
    if not s.ensure_ready("claim"):
        return {"success": False, "error": "session chưa sẵn sàng"}
    s.eval(JS_CREATE_TABLE, retries=0)
    time.sleep(2)
    s.eval(JS_SELECT_RADIO_11, retries=0)
    time.sleep(0.5)
    s.eval(JS_CLICK_CREATE, retries=0)
    time.sleep(3)
    s.eval(JS_CLICK_WATCH, retries=0)
    time.sleep(1)
    result = s.eval(JS_CLAIM, 15000, default={"success": False, "error": "evaluate failed"}, retries=0)
    if not isinstance(result, dict):
        result = {"success": False, "error": "bad result"}
    return result


def send_surrender(s: GameSession):
    """Gửi SURRENDER qua OutboundMessage trên WebSocket."""
    if not s.ensure_ready("surrender"):
        return {"ok": False, "error": "session chưa sẵn sàng"}
    s.eval(JS_INSTALL_WS_TAP, retries=0)
    result = s.eval(JS_SEND_SURRENDER, default={"ok": False, "error": "evaluate failed"}, retries=0)
    if not isinstance(result, dict):
        result = {"ok": False, "error": "bad result"}
    ok = result.get("ok", False) or result.get("err_code") == 22
    return {"ok": ok, **result}


def send_ping(s: GameSession):
    """Gửi PING keepalive qua OutboundMessage."""
    if not s.ensure_ready("ping"):
        return {"ok": False, "error": "session chưa sẵn sàng"}
    result = s.eval(JS_SEND_PING, default={"ok": False, "error": "evaluate failed"}, retries=0)
    if not isinstance(result, dict):
        result = {"ok": False, "error": "bad result"}
    return result


def ensure_in_lobby(s: GameSession):
    """Dùng place.enter('Lobby') để về lobby — KHÔNG reload trang."""
    state = s.eval(JS_GET_STATE, default={}, retries=0)
    path = state.get("path")
    if path == "Lobby":
        return True
    log(f"  [nav] path={path} — place.enter('Lobby')...")
    result = s.eval(JS_GO_LOBBY, retries=0)
    log(f"  [nav] place.enter result: {result}")
    time.sleep(3)
    state = s.eval(JS_GET_STATE, default={}, retries=0)
    return state.get("path") == "Lobby"


def surrender_one_round(s: GameSession, max_tables_to_scan=MAX_TABLES_TO_SCAN,
                        target_wait_seconds=TARGET_WAIT_SECONDS, bet_max=SURRENDER_BET_MAX):
    """Find target table via WS → join → SURRENDER."""
    log("\n[SURRENDER] tìm target via WebSocket...")
    bal_before, _ = s.get_balance()
    log(f"  balance trước: {bal_before:,}")

    found = None
    deadline = time.time() + target_wait_seconds
    attempt = 0
    ensure_in_lobby(s)
    s.eval(JS_CLOSE_POPUPS, retries=0)
    time.sleep(1)

    while time.time() < deadline:
        attempt += 1
        found = s.find_target_by_id(TARGET_ID, bet_max, max_tables_to_scan=max_tables_to_scan)
        if found:
            break
        wait = min(5, max(1, deadline - time.time()))
        log(f"  [wait] attempt #{attempt}: chưa thấy target — chờ {wait:.0f}s...")
        time.sleep(wait)
        ensure_in_lobby(s)
        s.eval(JS_CLOSE_POPUPS, retries=0)
        time.sleep(0.5)

    if not found:
        log(f"  [!] target_id={TARGET_ID} không tìm thấy sau {target_wait_seconds}s")
        return False, 0

    log(f"  ✓ found: table {found['id']} bet={found['bet']} player={found['player']}")

    s.eval(JS_CLOSE_TABLE_POPUP, retries=0)
    time.sleep(0.5)

    # Click table
    log(f"  [3] click bàn {found['id']}...")
    s.eval(JS_CLICK_TABLE, found["id"], retries=0)
    time.sleep(5)

    # Click Vào chơi
    log("  [4] tìm 'Vào chơi'...")
    clicked = False
    for i in range(10):
        r = s.eval(JS_CLICK_VAO_CHOI, retries=0)
        if r and r.get("clicked"):
            log("    ✓ Vào chơi OK")
            clicked = True
            break
        time.sleep(0.5)
    if not clicked:
        log("    [!] Vào chơi không thấy — bàn đầy hoặc bị từ chối")
        return False, 0

    # Wait for Bắt đầu
    log("  [5] chờ 'Bắt đầu' (max 30s)...")
    found_start = False
    for i in range(15):
        r = s.eval(JS_FIND_BAT_DAU, retries=0)
        if r and r.get("found"):
            log(f"    ✓ Bắt đầu: {r.get('text')}")
            s.eval(JS_CLICK_BAT_DAU, retries=0)
            log("    → click Bắt đầu, chờ 5s...")
            time.sleep(5)
            found_start = True
            break
        time.sleep(2)
    if not found_start:
        log("    [!] Bắt đầu không thấy — SURRENDER anyway")

    # Send SURRENDER
    log("  [6] gửi SURRENDER qua WebSocket...")
    r = send_surrender(s)
    log(f"    result: {r}")

    # Close dialog + return to lobby
    log("  [7] đóng dialog + place.enter('Lobby')...")
    time.sleep(3)
    for _ in range(5):
        clicked = s.eval(JS_CLICK_OK, retries=0)
        if clicked and clicked.get("clicked"):
            log(f"    ✓ đóng dialog: {clicked}")
            time.sleep(1)
            break
        time.sleep(1)
    ensure_in_lobby(s)

    bal_after, _ = s.get_balance()
    delta = bal_after - bal_before
    log(f"  [done] balance: {bal_before:,} → {bal_after:,} (Δ={delta:+,})")
    if delta < 0:
        log(f"         ✓ lost {-delta:,} xu (chuyển cho target)")
    return True, max(0, -delta)


def run_session(p, fb_cookies, session_id, started_at):
    log(f"\n########## SESSION {session_id} ##########")
    context = None
    page = None
    profile_dir = None
    s = None
    total_reward = 0
    total_transferred = 0
    ok = 0
    fail_total = 0
    cookie_ok = True

    try:
        fp = al.get_or_create_fingerprint(os.path.basename(SINGLE_COOKIE_FILE) if al else "ck2.txt") if al else None
        log(f"[ANTI-BAN] Fingerprint: UA={fp['user_agent'][:60]}... viewport={fp['viewport']} locale={fp.get('locale', 'en-US')}" if fp else "[ANTI-BAN] không có anti_lock")

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

        context, profile_dir = ws_bridge.launch_private_context(
            p, headless=HEADLESS,
            args=al.stealth_launch_args() if al else ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            context_kwargs=context_kwargs,
        )

        try:
            _priv_page = context.new_page()
            ws_bridge.wipe_storage(context, _priv_page)
            _priv_page.close()
        except Exception as e:
            log(f"  [PRIVACY] wipe storage fail: {e}")

        ws_bridge.install_ws_hook(context)

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

        # [1] Login FB
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

        if WARMUP_ENABLED and al:
            log("[ANTI-BAN] Warm-up: scroll FB feed...")
            try:
                al.warm_up_account(page, max_s=15)
                log("  Warm-up OK")
            except Exception as e:
                log(f"  Warm-up fail (ignore): {e}")

        # [2] Open game
        log("[2] Open game...")
        try:
            page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"  ERROR goto game: {e}")
            return 0, 0, 0, 0, True
        page.wait_for_timeout(20000)

        if s.find_frame(max_wait=FRAME_WAIT) is None:
            log("  ⚠ Chưa thấy game frame — thử recover 1 lần (KHÔNG reload)")
            try:
                s.recover("frame không xuất hiện lúc khởi động")
            except SessionDead as e:
                log(f"  ❌ SESSION DEAD: {e}")
                return 0, 0, 0, 0, True
        log("  Game loaded")

        # [3] Wait WS
        log("[3] Wait WS...")
        if not s.wait_ws(WS_WAIT):
            log("  ⚠ WS chưa connect — recover (KHÔNG reload)")
            try:
                if not s.ensure_ready("ws khởi động"):
                    log("  ❌ Không kết nối được WS")
                    return 0, 0, 0, 0, True
            except SessionDead as e:
                log(f"  ❌ SESSION DEAD: {e}")
                return 0, 0, 0, 0, True
        log("  WS connected")

        # [ANTI-BAN] Daily quota
        if al:
            ok_quota, reason = al.check_daily_quota(SINGLE_COOKIE_FILE,
                max_claims=MAX_CLAIMS_PER_DAY,
                max_transfers=MAX_TRANSFERS_PER_DAY,
                max_amount=MAX_TRANSFER_AMOUNT_PER_DAY)
            if not ok_quota:
                log(f"  ⏸ [ANTI-BAN] Bỏ qua session: {reason}")
                return 0, 0, 0, 0, True
            al.record_session_start(SINGLE_COOKIE_FILE)

        bal_start, _ = s.get_balance()
        log(f"  Balance: {bal_start}")

        # ===== TEST MODE: test claim + surrender =====
        if TEST_MODE:
            log("\n===== TEST MODE: claim + surrender =====")

            # Install WS tap to capture handlers
            s.install_ws_tap()
            log("  [WS] handler listeners: " + ", ".join(WS_IN_CMD_HANDLERS))
            log("  [WS] outbound commands: " + ", ".join(WS_OUT_CMDS))

            # Test PING
            log("\n[TEST] PING...")
            ping_r = send_ping(s)
            log(f"  PING result: {ping_r}")

            # Test CLAIM (VIDEO_REWARD)
            log("\n[TEST] VIDEO_REWARD claim...")
            result = trigger_and_claim(s)
            if result.get("success") and result.get("amount", 0) > 0:
                amount = result["amount"]
                total_reward += amount
                ok += 1
                log(f"  ✅ CLAIM OK +{amount} xu")
            else:
                fail_total += 1
                log(f"  ❌ CLAIM FAIL: {result.get('error', 'unknown')}")

            # Test SURRENDER
            current_bal, _ = s.get_balance()
            bal_num = parse_balance_num(current_bal)
            log(f"\n[TEST] Balance now: {current_bal} ({bal_num:,} xu)")
            if bal_num >= CLAIM_MIN:
                log("  ✅ Đủ tiền cho SURRENDER test")
                surrender_ok, lost = surrender_one_round(s)
                if surrender_ok:
                    log(f"  ✅ SURRENDER OK — mất {-lost if lost else 0:,} xu")
                else:
                    log(f"  ❌ SURRENDER thất bại")
            else:
                log(f"  ⚠ Balance {bal_num:,} < {CLAIM_MIN:,} — chưa đủ claim đủ để test SURRENDER")
                log(f"  [TEST] claim thêm để đạt {CLAIM_MIN:,}...")
                while bal_num < CLAIM_MIN:
                    r = trigger_and_claim(s)
                    if r.get("success") and r.get("amount", 0) > 0:
                        total_reward += r["amount"]
                        ok += 1
                        log(f"    ✅ +{r['amount']} xu → {bal_num + r['amount']}")
                    else:
                        log(f"    ❌ claim fail: {r.get('error')}")
                        break
                    current_bal, _ = s.get_balance()
                    bal_num = parse_balance_num(current_bal)
                    al.human_delay(2.0, 3.0) if al else time.sleep(2)

                if bal_num >= CLAIM_MIN:
                    log(f"  Đủ {CLAIM_MIN:,} — test SURRENDER")
                    surrender_ok, lost = surrender_one_round(s)
                    if surrender_ok:
                        log(f"  ✅ SURRENDER OK — mất {-lost if lost else 0:,} xu")
                    else:
                        log(f"  ❌ SURRENDER thất bại")

            log("\n===== TEST COMPLETE =====")
            return total_reward, total_transferred, ok, fail_total, cookie_ok

        # ===== NORMAL MODE: claim batch + transfer =====
        log(f"\n[4] BATCH MODE: claim ~{CLAIM_BATCH} lần -> transfer -> lặp lại")
        claim_count = 0
        batch_no = 0
        consec_fail = 0
        dest_id = TRANSFER_DEST_ID

        while True:
            if s.time_left() <= MIN_TIME_LEFT:
                log(f"  ⏹ Hết thời gian MAX_RUNTIME, dừng session.")
                break
            batch_no += 1
            cur_batch = al.random_batch_size(CLAIM_BATCH, lo=max(10, CLAIM_BATCH-5), hi=CLAIM_BATCH+5) if al else CLAIM_BATCH

            # Pre-batch transfer
            bal_before_batch = s.get_bal()
            bal_num_bb = parse_balance_num(bal_before_batch)
            if bal_num_bb > PRE_CLAIM_TRANSFER_THRESHOLD:
                log(f"\n[Pre-batch] Balance {bal_num_bb:,} > {PRE_CLAIM_TRANSFER_THRESHOLD:,} -> transfer")
                r = transfer_all_xu(s, dest_id)
                if r.get("success"):
                    total_transferred += r.get("balance", 0)
                    log(f"  ✅ Pre-batch transfer {r.get('balance',0):,} xu -> {dest_id}")
                    if al: al.record_transfer(SINGLE_COOKIE_FILE, r.get("balance", 0))
                    time.sleep(2)
                else:
                    log(f"  ❌ Pre-batch transfer fail: {r.get('error')}")

            log(f"\n[Batch #{batch_no}] {cur_batch} claim (dest={dest_id})...")
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

                if al and (al.detect_soft_ban(result.get("error", "")) or
                           al.detect_soft_ban(result.get("message", ""))):
                    log("  🚫 [ANTI-BAN] Soft-ban trong claim — back-off 15 phút")
                    al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)
                    return total_reward, total_transferred, ok, fail_total, True

                if result.get("success") and result.get("amount", 0) > 0:
                    amount = result["amount"]
                    total_reward += amount
                    ok += 1
                    claim_count += 1
                    consec_fail = 0
                    if al: al.record_claim(SINGLE_COOKIE_FILE, amount)
                    al.human_delay(1.0, 0.8) if al else time.sleep(1)
                    bal_after = s.get_bal()
                    log(f"    ✅ +{amount} | {bal_before} -> {bal_after} | tổng={total_reward:,}")
                    if al and IDLE_EVERY_CLAIMS > 0:
                        try: al.maybe_idle_browse(page, claim_count, every=IDLE_EVERY_CLAIMS)
                        except Exception: pass
                else:
                    fail_total += 1
                    consec_fail += 1
                    err = result.get("error", "unknown")
                    log(f"    ❌ FAIL ({err}) — fail liên tiếp: {consec_fail}")
                    cur_bal_num = parse_balance_num(s.get_bal())
                    if cur_bal_num >= 50000:
                        log(f"  💸 Balance {cur_bal_num:,} >= 50k — chuyển ngay")
                        break
                    if consec_fail >= MAX_CONSEC_CLAIM_FAIL:
                        log(f"  ⚠ {consec_fail} fail liên tiếp -> recover 1 lần")
                        try:
                            if s.ensure_ready("quá nhiều claim fail", attempts=1):
                                consec_fail = 0
                            else:
                                log("  ❌ recover thất bại -> kết thúc session")
                                stop_session = True
                                break
                        except SessionDead:
                            raise
                    if al: al.human_delay(max(2.0, DELAY), max(4.0, DELAY + 3.0))
                    else: time.sleep(DELAY + random.uniform(0, 1.5))

            # Post-batch transfer
            bal_after_batch = s.get_bal()
            bal_after_num = parse_balance_num(bal_after_batch)
            if bal_after_num > 200:
                log(f"\n[Transfer] Balance: {bal_after_batch} -> transfer về {dest_id}...")
                r = transfer_all_xu(s, dest_id)
                if r.get("success"):
                    total_transferred += r.get("balance", 0)
                    log(f"  ✅ transfer {r.get('balance',0):,} xu -> {dest_id}")
                    if al: al.record_transfer(SINGLE_COOKIE_FILE, r.get("balance", 0))
                    time.sleep(2)
                else:
                    log(f"  ❌ Transfer FAIL: {r.get('error')}")

            if stop_session:
                break
            if al: al.human_delay(max(3.0, DELAY), max(8.0, DELAY + 5.0))
            else: time.sleep(DELAY)

    except SessionDead as e:
        if "cookie chết" in str(e) or "logout" in str(e) or "checkpoint" in str(e):
            cookie_ok = False
    except Exception as e:
        log(f"\n[SESSION {session_id}] ⚠ Lỗi: {type(e).__name__}: {str(e)[:160]}")
    finally:
        try:
            if context:
                try:
                    if ws_bridge and s is not None:
                        _frames = s.ws_capture()
                        _cmds = ws_bridge.capture_commands(s.ws_target())
                        _names = [c.get("name") for c in _cmds if c.get("name")]
                        log(f"  🔎 [WS] game frame: {len(_frames)} frame / {len(_cmds)} lệnh"
                            + (f" | {', '.join(_names[:8])}" if _names else ""))
                        _mf = ws_bridge.capture_frames(page)
                        if _mf:
                            log(f"  🔎 [WS] main frame: {len(_mf)} frame (FB gateway)")
                except Exception:
                    pass
                try:
                    ws_bridge.wipe_storage(context, page)
                except Exception:
                    pass
                if ws_bridge:
                    ws_bridge.close_private_context(context, profile_dir)
                else:
                    context.close()
                log("  🧹 Đã đóng browser + xoá private profile")
        except Exception:
            pass

    log(f"[SESSION {session_id}] Kết thúc | OK={ok} fail={fail_total} "
        f"| reward={total_reward:,} | transferred={total_transferred:,}")
    return total_reward, total_transferred, ok, fail_total, cookie_ok


def main():
    log("=" * 64)
    log("FB phom_tala bot — WS target finding + SURRENDER (no page refresh)")
    log("=" * 64)
    log(f"Config: TRANSFER_DEST_ID={TRANSFER_DEST_ID} TARGET_NAME={TARGET_NAME} TARGET_ID={TARGET_ID}")
    log(f"        CLAIM_BATCH={CLAIM_BATCH} CLAIM_MIN={CLAIM_MIN:,} CLAIM_MAX={CLAIM_MAX:,} BET_MAX={BET_MAX:,}")
    log(f"        TEST_MODE={TEST_MODE} HEADLESS={HEADLESS}")
    log(f"WS endpoint: {WS_URL}")
    log(f"WS handlers: {', '.join(WS_IN_CMD_HANDLERS)}")
    log(f"WS outbound: {', '.join(WS_OUT_CMDS)}")
    log("=" * 64)

    cookie_entries = load_single_cookie_set(SINGLE_COOKIE_FILE)
    if not cookie_entries:
        log(f"[STOP] Không nạp được cookie từ {SINGLE_COOKIE_FILE}")
        return 1

    entry = cookie_entries[0]
    fb_cookies = parse_cookie(entry["raw"])
    if not fb_cookies:
        log(f"[STOP] Cookie {entry['file']} parse rỗng.")
        return 1
    log(f"[COOKIE] ✅ Parse {len(fb_cookies)} cookies từ {entry['file']}")

    if al:
        ok_quota, reason = al.check_daily_quota(SINGLE_COOKIE_FILE,
            max_claims=MAX_CLAIMS_PER_DAY, max_transfers=MAX_TRANSFERS_PER_DAY,
            max_amount=MAX_TRANSFER_AMOUNT_PER_DAY)
        if not ok_quota:
            log(f"\n[ANTI-BAN] ⏸ Dừng: {reason}")
            return 0

    if al and SESSION_START_JITTER > 0:
        wait = al.random_session_offset(SESSION_START_JITTER)
        log(f"\n[ANTI-BAN] ⏳ Session start jitter: nghỉ {wait:.1f}s...")

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
            log(f"[Session {session_id}/{MAX_SESSIONS}] {time.strftime('%H:%M:%S')} | còn {int(left)}s")
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
                log("[STOP] Cookie hỏng — không mở session mới.")
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n[EXIT] Người dùng dừng bot.")
        sys.exit(130)
