#!/usr/bin/env python3
"""

  A) Fingerprint ngẫu nhiên NHƯNG cố định per-account:
     Mỗi cookie file (ck1.txt, ck2.txt ...) sẽ luôn dùng cùng 1 UA / viewport /
     locale / timezone / WebGL vendor — không đổi giữa các lần chạy → giống 1
     thiết bị thật, nhưng khác nhau giữa các nick → không bị cluster bot-farm.
     (v2) Locale + timezone luôn đi CẶP với nhau (vi-VN↔Asia/Ho_Chi_Minh ...),
     thêm screen / accept_language / color_scheme → fingerprint nhất quán hơn.

  B) Stealth JS injection:
     Xóa navigator.webdriver, fake plugins, fake chrome.runtime, override
     permissions.query, spoof WebGL vendor/renderer, fake hardwareConcurrency,
     spoof screen/outerWidth khớp viewport, chống leak WebRTC.
     (v2) Sửa bug attachShadow sai ở bản cũ, toString-safe, an toàn tuyệt đối.

  C) Human-like timing:
     - human_delay(min,max)   → phân phối tam giác (dồn về phía nhanh, thỉnh
       thoảng chậm) thay vì fixed 3.0s — giống người thật hơn
     - jitter_sleep(base,jit) → base + random jitter
     - random_batch_size(n)   → batch size biến thiên QUANH `base` (gaussian)
     - human_type / human_click → gõ phím + di chuột kiểu người (v2)

  D) Daily quota / state file:
     Theo dõi số claim / số transfer / số xu đã transfer mỗi ngày cho mỗi cookie.
     Vượt MAX_CLAIMS_PER_DAY → tự dừng, tránh spam server.
     (v2) Ghi state AN TOÀN: file-lock + atomic write → nhiều nick chạy song
     song không còn ghi đè mất quota của nhau.

  E) Soft-ban detection:
     Phát hiện keyword "rate limit" / "too many requests" / "tạm thời" / "khóa"
     trong response → back-off dài (15-30 phút) trước khi thử lại.
     (v2) Dùng regex có ranh giới từ + ngưỡng điểm số → giảm FALSE POSITIVE
     (HTML bình thường chứa chữ "spam"/"blocked" không còn bị chặn oan).
     Back-off TĂNG DẦN theo chuỗi soft-ban liên tiếp (1.6^n, tối đa 4 giờ).

  F) Transfer destination rotation:
     Đọc danh sách hub account từ transfer_dests.json → xoay vòng, không phải
     luôn gửi về cùng 1 dest_id (tránh hub-and-spoke detection).
     (v2) SỬA BUG: hash() của Python bị randomize mỗi process → trước đây dest
     KHÔNG cố định như comment. Giờ dùng md5 → ổn định vĩnh viễn.

  G) Random session offset:
     Sleep 0-180s ngẫu nhiên trước khi mở session đầu tiên → 8 nick không
     "đồng loạt" bật browser cùng 1 lúc.
"""
import os
import re
import json
import time
import random
import hashlib
import contextlib

_HERE = os.path.dirname(os.path.abspath(__file__))
FP_DIR        = os.environ.get("FP_DIR", os.path.join(_HERE, "fingerprints"))
STATE_DIR     = os.environ.get("STATE_DIR", os.path.join(_HERE, "state"))
DEST_FILE     = os.environ.get("TRANSFER_DEST_FILE", os.path.join(_HERE, "transfer_dests.json"))

# =====================================================================
# A) FINGERPRINT POOLS
# =====================================================================
# Lấy từ thống kê Chrome thật phổ biến trên Windows/Mac/Linux.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1680, "height": 1050},
    {"width": 1366, "height": 768},
    {"width": 1600, "height": 900},
    {"width": 1280, "height": 720},
]

# Locale "vi-VN" nên dùng cho cookie Việt Nam; để fallback en-US cho an toàn.
LOCALES = ["en-US", "vi-VN", "en-GB", "en-SG"]

TIMEZONES = [
    "Asia/Ho_Chi_Minh", "Asia/Bangkok", "Asia/Singapore",
    "America/New_York", "Europe/London",
]

# (v2) Cặp locale ↔ timezone nhất quán — tránh "người Việt nhưng máy ở New York".
COHERENT_LOCALE_TZ = [
    ("vi-VN", "Asia/Ho_Chi_Minh"),
    ("en-US", "America/New_York"),
    ("en-GB", "Europe/London"),
    ("en-SG", "Asia/Singapore"),
    ("en-US", "Asia/Ho_Chi_Minh"),
]

PLATFORMS = {
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)":  "Win32",
    "Mozilla/5.0 (Macintosh;":                    "MacIntel",
    "Mozilla/5.0 (X11; Linux":                    "Linux x86_64",
}

WEBGL_VENDORS = [
    ("Google Inc. (Intel)",       "ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.1)"),
    ("Google Inc. (NVIDIA)",      "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060, OpenGL 4.5)"),
    ("Google Inc. (AMD)",          "ANGLE (AMD, AMD Radeon RX 580, OpenGL 4.5)"),
    ("Google Inc. (Intel)",        "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, OpenGL 4.1)"),
]


def _platform_for(ua: str) -> str:
    for k, v in PLATFORMS.items():
        if k in ua:
            return v
    return "Win32"


def _accept_language(locale: str) -> str:
    """Sinh Accept-Language header khớp locale (v2)."""
    primary = (locale or "en-US").split("-")[0]
    return f"{locale},{primary};q=0.9,en;q=0.8"


def _screen_for(viewport: dict) -> dict:
    """Screen size = viewport + taskbar/menubar (v2 — nhất quán với viewport)."""
    w = int(viewport.get("width", 1920))
    h = int(viewport.get("height", 1080))
    return {
        "width":       w,
        "height":      h + 48,          # taskbar / dock
        "availWidth":  w,
        "availHeight": h,
    }


def _normalize_fp(fp: dict) -> dict:
    """(v2) Điền field thiếu cho fingerprint cũ + fix field sai kiểu → migration."""
    if not isinstance(fp, dict):
        fp = {}
    ua = fp.get("user_agent") or random.choice(USER_AGENTS)
    vp = fp.get("viewport")
    if not isinstance(vp, dict) or "width" not in vp or "height" not in vp:
        vp = random.choice(VIEWPORTS)
    locale = fp.get("locale") or random.choice(LOCALES)
    if not fp.get("webgl_vendor") or not fp.get("webgl_renderer"):
        vendor, renderer = random.choice(WEBGL_VENDORS)   # luôn đi thành CẶP
    else:
        vendor, renderer = fp.get("webgl_vendor"), fp.get("webgl_renderer")
    fp.update({
        "user_agent":   ua,
        "viewport":     vp,
        "locale":       locale,
        "timezone":     fp.get("timezone") or random.choice(TIMEZONES),
        "platform":     fp.get("platform") or _platform_for(ua),
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "hardware_concurrency": fp.get("hardware_concurrency") or 8,
        "device_memory":        fp.get("device_memory") or 8,
        "color_scheme":  fp.get("color_scheme") or "light",
        "accept_language": fp.get("accept_language") or _accept_language(locale),
        "screen":        fp.get("screen") or _screen_for(vp),
        "created_at":    fp.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return fp


# =====================================================================
# Tiện ích ghi JSON an toàn (atomic + file lock) — dùng cho state/fingerprint
# =====================================================================
def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _atomic_write_json(path: str, data):
    """Ghi JSON qua temp file + os.replace → không bao giờ ghi dở/hỏng file."""
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        tmp = path + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(path + f".tmp.{os.getpid()}")
        except Exception:
            pass


@contextlib.contextmanager
def _file_lock(path: str):
    """Lock file (fcntl trên POSIX, fallback no-op) — chống race khi 8 nick chạy
    song song cùng đọc/ghi daily_state.json."""
    fh = None
    try:
        fh = open(path + ".lock", "w")
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except Exception:
            pass
        yield
    finally:
        if fh is not None:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def get_or_create_fingerprint(cookie_file: str) -> dict:
    """Trả về fingerprint cố định cho 1 cookie file. Tự sinh + lưu nếu chưa có."""
    os.makedirs(FP_DIR, exist_ok=True)
    fp_path = os.path.join(FP_DIR, "fingerprint.json")

    key = os.path.basename(cookie_file)
    with _file_lock(fp_path):
        fingerprints = _read_json(fp_path, {})
        if not isinstance(fingerprints, dict):
            fingerprints = {}

        dirty = False
        if key not in fingerprints:
            ua = random.choice(USER_AGENTS)
            vendor, renderer = random.choice(WEBGL_VENDORS)
            locale, timezone = random.choice(COHERENT_LOCALE_TZ)
            viewport = random.choice(VIEWPORTS)
            fingerprints[key] = {
                "user_agent":   ua,
                "viewport":     viewport,
                "locale":       locale,
                "timezone":     timezone,
                "platform":     _platform_for(ua),
                "webgl_vendor": vendor,
                "webgl_renderer": renderer,
                "hardware_concurrency": random.choice([4, 8, 8, 12, 16]),
                "device_memory": random.choice([4, 8, 8, 16]),
                "color_scheme": random.choice(["light", "light", "dark"]),
                "accept_language": _accept_language(locale),
                "screen":       _screen_for(viewport),
                "created_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            dirty = True
        else:
            completed = _normalize_fp(dict(fingerprints[key]))
            if completed != fingerprints[key]:
                fingerprints[key] = completed
                dirty = True

        if dirty:
            _atomic_write_json(fp_path, fingerprints)

    return fingerprints[key]


# =====================================================================
# B) STEALTH JS — inject vào mọi frame trước khi page load
# =====================================================================
# Dùng template + placeholder thay vì f-string → tránh lỗi escape ngoặc nhọn JS.
_STEALTH_JS_TEMPLATE = """() => {
    const FP = __FP_JSON__;
    try {
        // 1. navigator.webdriver = undefined
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});
        try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}

        // 2. navigator.platform khớp với UA
        Object.defineProperty(navigator, 'platform', {get: () => FP.platform, configurable: true});

        // 3. navigator.plugins — Chrome thật có plugin PDF
        const mkMime = (type, suffixes, description) => {
            const m = {type, suffixes, description};
            m.enabledPlugin = null;
            return m;
        };
        const mkPlugin = (name, filename, description, mime) => {
            const p = {name, filename, description, length: 1};
            p[0] = mime;
            mime.enabledPlugin = p;
            p.item = (i) => p[i] || null;
            p.namedItem = (n) => (n === mime.type ? mime : null);
            p.refresh = () => {};
            return p;
        };
        const pdfMime = mkMime('application/pdf', 'pdf', 'Portable Document Format');
        const pdfPlugin = mkPlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format', pdfMime);
        const pdfViewer = mkPlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', mkMime('application/pdf', 'pdf', ''));
        const nacl = mkPlugin('Native Client', 'internal-nacl-plugin', '', mkMime('application/x-nacl', '', 'Native Client Executable'));
        const fakePlugins = [pdfPlugin, pdfViewer, nacl];
        fakePlugins.item = (i) => fakePlugins[i] || null;
        fakePlugins.namedItem = (n) => ({'Chrome PDF Plugin': pdfPlugin, 'Chrome PDF Viewer': pdfViewer, 'Native Client': nacl}[n] || null);
        fakePlugins.refresh = () => {};
        Object.defineProperty(navigator, 'plugins', {get: () => fakePlugins, configurable: true});

        // 4. navigator.language / languages khớp fingerprint
        Object.defineProperty(navigator, 'language', {get: () => FP.locale, configurable: true});
        Object.defineProperty(navigator, 'languages', {
            get: () => Object.freeze([FP.locale, FP.locale.split('-')[0], 'en']),
            configurable: true
        });

        // 5. navigator.hardwareConcurrency / deviceMemory / maxTouchPoints
        Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => FP.hardware_concurrency, configurable: true});
        Object.defineProperty(navigator, 'deviceMemory', {get: () => FP.device_memory, configurable: true});
        Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0, configurable: true});

        // 6. window.chrome runtime (Playwright sẽ không có sẵn)
        if (!window.chrome) {
            window.chrome = {
                runtime: {id: undefined, connect: () => {}, sendMessage: () => {}},
                app: {isInstalled: false,
                      InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'},
                      RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'}},
                csi: () => {},
                loadTimes: () => {}
            };
        }

        // 7. Override permissions.query để trả về Notification.permission
        const origQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (origQuery) {
            window.navigator.permissions.query = (parameters) => {
                if (parameters && parameters.name === 'notifications') {
                    return Promise.resolve({state: (typeof Notification !== 'undefined') ? Notification.permission : 'default'});
                }
                return origQuery.call(window.navigator.permissions, parameters);
            };
            // patch toString để không bị phát hiện qua Function.prototype.toString
            const origToString = Function.prototype.toString;
            window.navigator.permissions.query.toString = () => origToString.call(origQuery);
            Function.prototype.toString = function() {
                if (this === window.navigator.permissions.query) return 'function query() { [native code] }';
                return origToString.call(this);
            };
        }

        // 8. WebGL fingerprint spoofing (WebGL1 + WebGL2)
        const spoofGL = (proto) => {
            if (!proto || !proto.getParameter) return;
            const origGetParameter = proto.getParameter;
            proto.getParameter = function(parameter) {
                // UNMASKED_VENDOR_WEBGL = 37445, UNMASKED_RENDERER_WEBGL = 37446
                if (parameter === 37445) return FP.webgl_vendor;
                if (parameter === 37446) return FP.webgl_renderer;
                return origGetParameter.call(this, parameter);
            };
        };
        spoofGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
        spoofGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

        // 9. Screen / outerWindow nhất quán với viewport (trước đây bản cũ thiếu)
        if (FP.screen) {
            const def = (obj, prop, val) => {
                try { Object.defineProperty(obj, prop, {get: () => val, configurable: true}); } catch (e) {}
            };
            def(screen, 'width', FP.screen.width);
            def(screen, 'height', FP.screen.height);
            def(screen, 'availWidth', FP.screen.availWidth);
            def(screen, 'availHeight', FP.screen.availHeight);
            def(screen, 'colorDepth', 24);
            def(screen, 'pixelDepth', 24);
            def(window, 'outerWidth', FP.screen.width);
            def(window, 'outerHeight', FP.screen.height + 37);
        }

        // 10. Hide WebRTC IP leak (để proxy không bị leak IP thật)
        if (window.RTCPeerConnection) {
            const origRTC = window.RTCPeerConnection;
            window.RTCPeerConnection = function(...args) {
                if (args[0] && args[0].iceServers) {
                    args[0].iceServers = [];
                }
                return new origRTC(...args);
            };
            window.RTCPeerConnection.prototype = origRTC.prototype;
        }
    } catch (e) {
        // stealth fail silent — không crash game
    }
}"""


def build_stealth_js(fp: dict) -> str:
    """Trả về JS string để add_init_script(). Nhận fingerprint dict."""
    fp = _normalize_fp(dict(fp or {}))
    return _STEALTH_JS_TEMPLATE.replace("__FP_JSON__", json.dumps(fp, ensure_ascii=False))


# =====================================================================
# C) HUMAN-LIKE TIMING
# =====================================================================
def human_delay(min_s: float = 2.0, max_s: float = 6.0):
    """Sleep với phân phối tam giác — dồn về phía nhanh, thỉnh thoảng chậm
    (giống người: hầu hết cú click nhanh, lâu lâu mới ngập ngừng)."""
    if max_s < min_s:
        min_s, max_s = max_s, min_s
    delay = random.triangular(min_s, max_s, min_s + (max_s - min_s) * 0.3)
    time.sleep(delay)


def jitter_sleep(base_s: float, jitter_s: float = 0.6):
    """Base delay + jitter ngẫu nhiên. Dùng cho các chờ nhỏ giữa claim."""
    time.sleep(max(0.1, base_s + random.uniform(0, max(0.0, jitter_s))))


def random_batch_size(base: int = 40, lo: int = 25, hi: int = 55) -> int:
    """Trả về batch size ngẫu nhiên QUANH `base` (gaussian, clamp [lo,hi]),
    tránh pattern fixed-40 mà vẫn tôn trọng `base` người dùng cấu hình."""
    lo = max(5, min(lo, hi))
    hi = max(lo, hi)
    if base < lo:
        base = lo
    if base > hi:
        base = hi
    sigma = max(1.0, (hi - lo) / 6.0)
    val = int(round(random.gauss(base, sigma)))
    return max(5, min(hi, max(lo, val)))


def random_session_offset(max_s: int = 180) -> float:
    """Sleep 0..max_s giây trước khi mở session đầu tiên."""
    wait = random.uniform(0, max_s)
    time.sleep(wait)
    return wait


# =====================================================================
# C2) HUMAN-LIKE INPUT (v2) — gõ phím / click chuột kiểu người
# =====================================================================
def human_type(page, selector, text: str,
               per_key_min: float = 0.05, per_key_max: float = 0.22,
               think_pause_every: tuple = (6, 14)):
    """Gõ text từng ký tự với delay không đều + thỉnh thoảng 'nghĩ' 0.3-0.9s.

    selector: chuỗi CSS hoặc Locator. Trả về True/False.
    """
    try:
        el = page.locator(selector) if isinstance(selector, str) else selector
        el.click()
        time.sleep(random.uniform(0.2, 0.6))
        next_pause = random.randint(*think_pause_every)
        for i, ch in enumerate(text):
            page.keyboard.type(ch)
            if i >= next_pause:
                time.sleep(random.uniform(0.3, 0.9))   # pause "nghĩ"
                next_pause = i + random.randint(*think_pause_every)
            else:
                time.sleep(random.uniform(per_key_min, per_key_max))
        return True
    except Exception:
        return False


def human_click(page, selector) -> bool:
    """Di chuột tới vị trí lệch nhẹ trong element rồi click — không teleport."""
    try:
        el = page.locator(selector) if isinstance(selector, str) else selector
        box = el.bounding_box()
        if box:
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            page.mouse.move(x, y, steps=random.randint(5, 18))
            time.sleep(random.uniform(0.05, 0.25))
            page.mouse.click(x, y)
        else:
            el.click()
        return True
    except Exception:
        return False


# =====================================================================
# D) DAILY QUOTA / STATE
# =====================================================================
DEFAULT_MAX_CLAIMS_PER_DAY    = 180   # ~3 batch x 40 + buffer
DEFAULT_MAX_TRANSFERS_PER_DAY = 30
DEFAULT_MAX_TRANSFER_AMOUNT   = 2_000_000  # tổng xu transfer/ngày/cookie

_STATE_FILE = os.path.join(STATE_DIR, "daily_state.json")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _fresh_state() -> dict:
    return {
        "date":               _today(),
        "claims_today":       0,
        "transfers_today":    0,
        "amount_transferred": 0,
        "sessions_today":     0,
        "last_run_ts":        0,
        "soft_ban_until":     0,    # epoch seconds — nếu > now thì đang bị soft-ban
        "soft_ban_streak":    0,    # (v2) số lần soft-ban liên tiếp → back-off tăng dần
    }


def load_daily_state(cookie_file: str) -> dict:
    os.makedirs(STATE_DIR, exist_ok=True)
    key = os.path.basename(cookie_file)
    with _file_lock(_STATE_FILE):
        states = _read_json(_STATE_FILE, {})
        if not isinstance(states, dict):
            states = {}
        entry = states.get(key)
        if not isinstance(entry, dict) or entry.get("date") != _today():
            entry = _fresh_state()
            states[key] = entry
            _atomic_write_json(_STATE_FILE, states)
    return entry


def _save_state(state_path: str, states: dict):
    _atomic_write_json(state_path, states)


def save_daily_state(cookie_file: str, state: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    key = os.path.basename(cookie_file)
    with _file_lock(_STATE_FILE):
        states = _read_json(_STATE_FILE, {})
        if not isinstance(states, dict):
            states = {}
        states[key] = state
        _atomic_write_json(_STATE_FILE, states)


def _update_state(cookie_file: str, mutate) -> dict:
    """(v2) Read-modify-write NGUYÊN TỬ dưới lock — an toàn khi chạy song song."""
    os.makedirs(STATE_DIR, exist_ok=True)
    key = os.path.basename(cookie_file)
    with _file_lock(_STATE_FILE):
        states = _read_json(_STATE_FILE, {})
        if not isinstance(states, dict):
            states = {}
        entry = states.get(key)
        if not isinstance(entry, dict) or entry.get("date") != _today():
            entry = _fresh_state()
        entry = dict(entry)
        mutate(entry)
        states[key] = entry
        _atomic_write_json(_STATE_FILE, states)
    return entry


def check_daily_quota(cookie_file: str,
                      max_claims=DEFAULT_MAX_CLAIMS_PER_DAY,
                      max_transfers=DEFAULT_MAX_TRANSFERS_PER_DAY,
                      max_amount=DEFAULT_MAX_TRANSFER_AMOUNT) -> tuple:
    """Trả về (ok, reason). ok=False nghĩa là đã vượt quota hôm nay."""
    st = load_daily_state(cookie_file)
    if st["claims_today"] >= max_claims:
        return False, f"đã claim {st['claims_today']}/{max_claims} hôm nay"
    if st["transfers_today"] >= max_transfers:
        return False, f"đã transfer {st['transfers_today']}/{max_transfers} lần hôm nay"
    if st["amount_transferred"] >= max_amount:
        return False, f"đã transfer {st['amount_transferred']:,}/{max_amount:,} xu hôm nay"
    if st.get("soft_ban_until", 0) > time.time():
        wait = int(st["soft_ban_until"] - time.time())
        return False, f"đang bị soft-ban, còn {wait}s chờ"
    return True, "ok"


def record_claim(cookie_file: str, amount: int = 0):
    def _m(st):
        st["claims_today"] = st.get("claims_today", 0) + 1
        if amount:
            st["coins_claimed"] = st.get("coins_claimed", 0) + int(amount)
        # claim thành công → reset chuỗi soft-ban
        st["soft_ban_streak"] = 0
    _update_state(cookie_file, _m)


def record_transfer(cookie_file: str, amount: int = 0):
    def _m(st):
        st["transfers_today"] = st.get("transfers_today", 0) + 1
        st["amount_transferred"] = st.get("amount_transferred", 0) + int(amount or 0)
    _update_state(cookie_file, _m)


def record_session_start(cookie_file: str):
    def _m(st):
        st["sessions_today"] = st.get("sessions_today", 0) + 1
        st["last_run_ts"] = int(time.time())
    _update_state(cookie_file, _m)


def set_soft_ban(cookie_file: str, duration_s: int = 900, escalate: bool = True):
    """Đặt cooldown soft-ban (mặc định 15 phút).

    (v2) escalate=True: mỗi lần soft-ban liên tiếp nhân 1.6 lần thời gian chờ
    (tối đa 4 giờ) — FB thấy mình 'biết điều' thay vì retry đều đều.
    """
    def _m(st):
        if escalate:
            streak = int(st.get("soft_ban_streak", 0)) + 1
            duration = int(min(duration_s * (1.6 ** (streak - 1)), 4 * 3600))
            st["soft_ban_streak"] = streak
        else:
            duration = int(duration_s)
        st["soft_ban_until"] = int(time.time()) + duration
    _update_state(cookie_file, _m)


def clear_soft_ban(cookie_file: str):
    """Xoá soft-ban + reset streak (gọi khi xác nhận nick đã hoạt động bình thường)."""
    def _m(st):
        st["soft_ban_until"] = 0
        st["soft_ban_streak"] = 0
    _update_state(cookie_file, _m)


# =====================================================================
# E) SOFT-BAN / RATE-LIMIT DETECTION
# =====================================================================
SOFT_BAN_HINTS = (
    "rate limit", "too many requests", "throttle", "spam",
    "tạm thời", "vui lòng thử lại", "khóa tạm", "bị hạn chế",
    "temporarily blocked", "temporarily restricted", "unusual activity",
    "vui lòng đợi", "đăng nhập lại", "session expired", "blocked",
    "khóa", "cấm",
)

# (v2) Regex có ranh giới từ — "blocked" trong link/CSS không còn match bừa.
SOFT_BAN_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"rate[\s_-]*limit(ed)?",
    r"too\s+many\s+requests",
    r"throttl",
    r"\bspam(ming)?\b",
    r"tạm\s*thời",
    r"vui\s*lòng\s*thử\s*lại",
    r"khóa\s*tạm",
    r"bị\s*hạn\s*chế",
    r"temporarily\s+(blocked|restricted)",
    r"unusual\s+activity",
    r"vui\s*lòng\s*đợi",
    r"session\s+(expired|invalid)",
    r"(account|user|bạn)\s+(has\s+been\s+)?(blocked|disabled)",
    r"bị\s*(khóa|cấm|chặn)",
    r"khóa\s*(nick|tài\s*khoản)",
    r"checkpoint\s+required",
))


def detect_soft_ban_detail(text: str):
    """(v2) Trả về pattern soft-ban match đầu tiên, hoặc None.

    Quy tắc chống FALSE POSITIVE:
      - text ngắn (<1500 ký tự): 1 match là đủ (đây thường là thông báo lỗi).
      - text dài (nguyên trang HTML): cần ≥2 pattern KHÁC NHAU match → mới chặn.
    """
    if not text:
        return None
    t = str(text)
    hits = [p for p in SOFT_BAN_PATTERNS if p.search(t)]
    if not hits:
        return None
    threshold = 2 if len(t) > 1500 else 1
    if len(hits) >= threshold:
        return hits[0].pattern
    return None


def detect_soft_ban(text: str) -> bool:
    return detect_soft_ban_detail(text) is not None


# =====================================================================
# F) TRANSFER DESTINATION ROTATION
# =====================================================================
def load_transfer_dests(default_id: int = 51977054) -> list:
    """Load danh sách dest_id từ transfer_dests.json. Fallback [default_id] nếu lỗi."""
    if not os.path.exists(DEST_FILE):
        return [default_id]
    try:
        with open(DEST_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        dests = data.get("dests") or data.get("destinations") or []
        if not dests:
            return [default_id]
        return [int(d) for d in dests if d]
    except Exception:
        return [default_id]


def pick_next_dest(cookie_file: str, default_id: int = 51977054) -> int:
    """Xoay vòng dest_id per-cookie: cookie N luôn đi về dest[N % len(dests)].

    Lý do: nếu 8 nick cùng transfer về 1 dest thì đó là hub-and-spoke pattern
    cực kỳ dễ bị Facebook phát hiện (graph anomaly). Xoay vòng giúp phân tán.

    (v2) SỬA BUG: bản cũ dùng hash() — Python randomize hash seed mỗi process
    nên dest đổi liên tục, PHÁ VỠ tính "cố định per-cookie". Giờ dùng md5 →
    index luôn ổn định giữa các lần chạy.
    """
    dests = load_transfer_dests(default_id)
    if len(dests) <= 1:
        return dests[0]
    # md5(key) → index cố định vĩnh viễn, không phụ thuộc PYTHONHASHSEED
    key = os.path.basename(cookie_file).encode("utf-8")
    digest = hashlib.md5(key).hexdigest()
    idx = int(digest[:8], 16) % len(dests)
    # Thêm jitter: đôi khi chọn dest kế bên để trông tự nhiên hơn
    if random.random() < 0.15:
        idx = (idx + 1) % len(dests)
    return dests[idx]


# =====================================================================
# G) BROWSER LAUNCH ARGS (chống detect + chống leak)
# =====================================================================
def stealth_launch_args(fp: dict = None) -> list:
    """Args cho Chromium launch — mạnh hơn --disable-blink-features.

    (v2) Nhận optional fp dict: thêm --lang / --window-size khớp fingerprint
    (gọi không tham số vẫn hoạt động như cũ).
    """
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        # Bỏ cờ automation
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        # Tắt WebRTC leak IP thật khi chạy qua proxy
        "--disable-webrtc-multiple-routes",
        "--disable-webrtc-pc2-experiment",
        "--enforce-webrtc-ip-permission-check",
        # Tắt notification (FB có thể pop-up)
        "--disable-notifications",
        # Tắt một số tính năng gây fingerprint lạ
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        # Số port DevTools ngẫu nhiên để không bị fingerprint qua port cố định
        "--disable-default-apps",
        # GPU info leak
        "--disable-gpu-sandbox",
    ]
    if isinstance(fp, dict):
        try:
            if fp.get("locale"):
                args.append(f"--lang={fp['locale']}")
            vp = fp.get("viewport") or {}
            if vp.get("width") and vp.get("height"):
                args.append(f"--window-size={vp['width']},{vp['height']}")
        except Exception:
            pass
    return args


# =====================================================================
# H) HUMAN-LIKE IDLE — tạo "session nghỉ" ngắn giữa các batch
# =====================================================================
def maybe_idle_browse(page, max_claims_so_far: int, every: int = 60):
    """Mỗi ~`every` claim (± jitter), giả lập người dùng nghỉ 30-90s (lướt FB feed).

    Tránh pattern "claim liên tục 5.5 giờ không nghỉ" — server sẽ flag.
    (v2) Ngưỡng nghỉ cũng được jitter → không phải lúc nào cũng đúng % 60.
    """
    if max_claims_so_far <= 0:
        return
    every_now = max(10, every + random.randint(-10, 15))
    if max_claims_so_far % every_now != 0:
        return
    wait = random.uniform(30, 90)
    print(f"  😴 Đã claim {max_claims_so_far} lần — nghỉ {wait:.0f}s giả lập người dùng.",
          flush=True)
    # Optionally scroll để giống người lướt feed
    try:
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(100, 400))
            time.sleep(random.uniform(0.8, 2.5))
    except Exception:
        pass
    time.sleep(wait)


def warm_up_account(page, fb_url: str = "https://www.facebook.com/", max_s: int = 15):
    """Sau khi login, scroll FB feed ~max_s giây trước khi vào game — giống người thật.

    (v2) Tôn trọng ngân sách `max_s` (bản cũ bỏ qua tham số này → có thể lướt
    lâu hơn dự kiến). Trả về tổng số giây đã warm-up.
    """
    deadline = time.time() + max(1.0, float(max_s))
    elapsed = 0.0
    start = time.time()
    try:
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining < 1.0:
                break
            page.mouse.wheel(0, random.randint(200, 800))
            time.sleep(min(random.uniform(1.5, 4.0), max(0.1, remaining)))
            # Thỉnh thoảng di chuột tới 1 vị trí ngẫu nhiên (hover)
            if random.random() < 0.5:
                try:
                    page.mouse.move(random.randint(200, 1500), random.randint(200, 800),
                                    steps=random.randint(4, 12))
                    time.sleep(min(random.uniform(0.8, 2.0), max(0.1, deadline - time.time())))
                except Exception:
                    pass
    except Exception:
        # Ignore — không phải lỗi nghiêm trọng
        pass
    elapsed = time.time() - start
    return elapsed


# =====================================================================
# Self-test khi chạy trực tiếp
# =====================================================================
if __name__ == "__main__":
    print("=== anti_lock.py self-test (v2) ===")
    fp = get_or_create_fingerprint("ck_test.txt")
    print("Fingerprint for ck_test.txt:")
    print(json.dumps(fp, indent=2, ensure_ascii=False))
    assert fp["locale"].split("-")[0] and fp["accept_language"]
    print("\nStealth JS preview (first 200 chars):")
    print(build_stealth_js(fp)[:200], "...")
    print("\nDaily quota check:")
    ok, reason = check_daily_quota("ck_test.txt")
    print(f"  ok={ok}  reason={reason}")
    print(f"\nTransfer dests loaded: {load_transfer_dests()}")
    d1 = pick_next_dest("ck_test.txt")
    d2 = pick_next_dest("ck_test.txt")
    print(f"Next dest for ck_test.txt: {d1} (gọi lại: {d2})")
    print(f"Launch args count: {len(stealth_launch_args(fp))}")

    print("\nSoft-ban detection (kỳ vọng: True True False False True False):")
    print("  'Rate limited, too many requests'      →", detect_soft_ban("Rate limited, too many requests"))
    print("  'Tài khoản của bạn bị khóa tạm thời'    →", detect_soft_ban("Tài khoản của bạn bị khóa tạm thời"))
    print("  'Chúc bạn chơi game vui vẻ!'           →", detect_soft_ban("Chúc bạn chơi game vui vẻ!"))
    print("  HTML dài có chữ 'spam' đơn lẻ          →", detect_soft_ban("<html><body>" + "spam " + "x" * 2000 + "</body></html>"))
    print("  HTML dài chứa 'rate limit' + 'blocked'  →", detect_soft_ban("<html>rate limit " + "x" * 2000 + " account has been blocked</html>"))
    print("  None                                  →", detect_soft_ban(None))
    print("\nrandom_batch_size(40) mẫu:", [random_batch_size(40) for _ in range(5)])
    print("\n=== OK ===")
