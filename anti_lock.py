#!/usr/bin/env python3
import os
import re
import json
import time
import copy
import random
import hashlib
import contextlib

_HERE = os.path.dirname(os.path.abspath(__file__))
FP_DIR        = os.environ.get("FP_DIR", os.path.join(_HERE, "fingerprints"))
STATE_DIR     = os.environ.get("STATE_DIR", os.path.join(_HERE, "state"))
DEST_FILE     = os.environ.get("TRANSFER_DEST_FILE", os.path.join(_HERE, "transfer_dests.json"))

# Thời gian nghỉ sau mỗi đợt claim (giây) — tổng cả phần cuộn feed
IDLE_WAIT_MIN = float(os.environ.get("IDLE_WAIT_MIN", "10"))
IDLE_WAIT_MAX = float(os.environ.get("IDLE_WAIT_MAX", "30"))

# =====================================================================
# A) FINGERPRINT POOLS
# =====================================================================
# Chrome thật phổ biến trên Windows/Mac/Linux (2026).
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1680, "height": 1050},
    {"width": 1366, "height": 768},
    {"width": 1600, "height": 900},
    {"width": 1280, "height": 720},
    {"width": 1920, "height": 1200},
    {"width": 2560, "height": 1440},
    {"width": 1512, "height": 982},   # MacBook Pro 14"
    {"width": 1728, "height": 1117},  # MacBook Pro 16"
]

# Locale "vi-VN" nên dùng cho cookie Việt Nam; để fallback en-US cho an toàn.
LOCALES = ["en-US", "vi-VN", "en-GB", "en-SG"]

TIMEZONES = [
    "Asia/Ho_Chi_Minh", "Asia/Bangkok", "Asia/Singapore",
    "America/New_York", "Europe/London",
]

# Cặp locale ↔ timezone nhất quán — tránh "người Việt nhưng máy ở New York".
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
    ("Google Inc. (Intel)",       "ANGLE (Intel, Intel(R) UHD Graphics 770, OpenGL 4.1)"),
    ("Google Inc. (Intel)",       "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, OpenGL 4.1)"),
    ("Google Inc. (NVIDIA)",      "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060, OpenGL 4.5)"),
    ("Google Inc. (NVIDIA)",      "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060, OpenGL 4.5)"),
    ("Google Inc. (NVIDIA)",      "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060, OpenGL 4.5)"),
    ("Google Inc. (AMD)",         "ANGLE (AMD, AMD Radeon RX 580, OpenGL 4.5)"),
    ("Google Inc. (AMD)",         "ANGLE (AMD, AMD Radeon RX 6600 XT, OpenGL 4.5)"),
    ("Google Inc. (Apple)",       "ANGLE (Apple, Apple M2, OpenGL 4.1)"),
]


def _platform_for(ua: str) -> str:
    for k, v in PLATFORMS.items():
        if k in ua:
            return v
    return "Win32"


def _accept_language(locale: str) -> str:
    """Sinh Accept-Language header khớp locale."""
    primary = (locale or "en-US").split("-")[0]
    return f"{locale},{primary};q=0.9,en;q=0.8"


def _screen_for(viewport: dict) -> dict:
    """Screen size = viewport + taskbar/menubar (nhất quán với viewport)."""
    w = int(viewport.get("width", 1920))
    h = int(viewport.get("height", 1080))
    return {
        "width":       w,
        "height":      h + 48,          # taskbar / dock
        "availWidth":  w,
        "availHeight": h,
    }


def _coherent_tz_for(locale: str) -> str:
    """Chọn timezone khớp locale (vi-VN ↔ Asia/Ho_Chi_Minh ...) — tránh
    'người Việt nhưng máy ở New York' khi field timezone bị thiếu."""
    for loc, tz in COHERENT_LOCALE_TZ:
        if loc == locale:
            return tz
    return random.choice(TIMEZONES)


def _normalize_fp(fp: dict) -> dict:
    """Điền field thiếu cho fingerprint cũ + fix field sai kiểu → migration."""
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
        "timezone":     fp.get("timezone") or _coherent_tz_for(locale),
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


# ---- SHARDING PER-BOT: mỗi bot 1 file riêng → 100+ bot không tranh chấp ----
def _bot_id(cookie_file: str) -> str:
    """'ck1.txt' → 'ck1.txt' (lọc ký tự lạ để làm tên file an toàn)."""
    return re.sub(r"[^\w.\-]", "_", os.path.basename(cookie_file or "unknown"))


def _fp_path(cookie_file: str) -> str:
    """File fingerprint RIÊNG của 1 bot: fingerprints/<cookie>.fp.json"""
    return os.path.join(FP_DIR, _bot_id(cookie_file) + ".fp.json")


def _bot_state_path(cookie_file: str) -> str:
    """File state RIÊNG của 1 bot: state/<cookie>.state.json"""
    return os.path.join(STATE_DIR, _bot_id(cookie_file) + ".state.json")


# Cache fingerprint in-process, validate theo mtime_ns: đọc lại file khi có
# process khác ghi (os.replace → mtime đổi), còn không thì bỏ qua lock + I/O.
_FP_CACHE: dict = {}


def _fp_cache_get(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _FP_CACHE.get(path)
    if hit and hit[0] == st.st_mtime_ns:
        return copy.deepcopy(hit[1])
    return None


def _fp_cache_put(path: str, fp: dict):
    try:
        st = os.stat(path)
        _FP_CACHE[path] = (st.st_mtime_ns, copy.deepcopy(fp))
    except OSError:
        pass


def _fp_build(fp_path: str, key: str):
    """Đọc + migrate từ file gộp cũ + normalize. Trả về (fp, dirty)."""
    fp = _read_json(fp_path, None)
    dirty = False

    if not isinstance(fp, dict) or not fp:
        legacy = _read_json(os.path.join(FP_DIR, "fingerprint.json"), {})
        fp = legacy.get(key) if isinstance(legacy, dict) else None
        if not isinstance(fp, dict):
            ua = random.choice(USER_AGENTS)
            vendor, renderer = random.choice(WEBGL_VENDORS)
            locale, timezone = random.choice(COHERENT_LOCALE_TZ)
            viewport = random.choice(VIEWPORTS)
            fp = {
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

    completed = _normalize_fp(dict(fp))
    if completed != fp:
        fp = completed
        dirty = True
    return fp, dirty


def get_or_create_fingerprint(cookie_file: str) -> dict:
    """Trả về fingerprint cố định cho 1 cookie file. Tự sinh + lưu nếu chưa có.

    Lưu PER-BOT (fingerprints/<cookie>.fp.json): mỗi bot 1 file → không lock
    toàn cục, không race. Đường ĐỌC không lock (ghi là atomic qua os.replace);
    chỉ lock khi cần tạo/migrate/normalize → lần gọi lặp lại lấy từ cache.
    """
    key = os.path.basename(cookie_file)
    fp_path = _fp_path(cookie_file)

    cached = _fp_cache_get(fp_path)
    if cached is not None:
        return cached

    fp, dirty = _fp_build(fp_path, key)
    if not dirty:
        _fp_cache_put(fp_path, fp)
        return fp

    os.makedirs(FP_DIR, exist_ok=True)
    with _file_lock(fp_path):
        fp, dirty = _fp_build(fp_path, key)   # re-read dưới lock
        if dirty:
            _atomic_write_json(fp_path, fp)
    _fp_cache_put(fp_path, fp)
    return fp


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


def bot_concurrency() -> int:
    """Số bot dự kiến chạy song song (env BOT_COUNT / CONCURRENCY / N_BOTS, mặc định 8)."""
    for env in ("BOT_COUNT", "CONCURRENCY", "N_BOTS"):
        v = os.environ.get(env)
        try:
            if v and int(v) > 0:
                return int(v)
        except Exception:
            pass
    return 8


def random_session_offset(max_s: int = 180) -> float:
    """Sleep 0..max_s giây trước khi mở session đầu tiên.

    Tự NỚI RỘNG cửa sổ theo số bot: ~12s/bot (100 bot → ~1200s spread)
    để nhiều browser không "đồng loạt" bật lên cùng lúc. Set env BOT_COUNT để
    chỉnh; min = max_s, max = 3600s.
    """
    spread = max(max_s, min(3600, bot_concurrency() * 12))
    wait = random.uniform(0, spread)
    time.sleep(wait)
    return wait


# =====================================================================
# C2) HUMAN-LIKE INPUT — gõ phím / click chuột kiểu người
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

_STATE_FILE = os.path.join(STATE_DIR, "daily_state.json")  # file gộp cũ — chỉ đọc để migrate


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
        "soft_ban_streak":    0,    # số lần soft-ban liên tiếp → back-off tăng dần
    }


def _load_entry(path: str, cookie_file: str):
    """Đọc entry state. Trả về (entry, dirty).

    - Tự migrate từ daily_state.json (file gộp cũ) nếu bot chưa có file riêng.
    - Tự bổ sung field mới (_fresh_state) cho state tạo bởi phiên bản trước.
    An toàn khi đọc không lock: mọi ghi đều qua os.replace (atomic).
    """
    entry = _read_json(path, None)
    dirty = False
    if not isinstance(entry, dict):
        legacy = _read_json(_STATE_FILE, {})
        entry = legacy.get(os.path.basename(cookie_file)) if isinstance(legacy, dict) else None
        dirty = True
    if not isinstance(entry, dict) or entry.get("date") != _today():
        entry = _fresh_state()
        dirty = True
    else:
        completed = dict(_fresh_state())
        completed.update(entry)
        if completed != entry:
            entry = completed
            dirty = True
    return entry, dirty


def load_daily_state(cookie_file: str) -> dict:
    """Đọc state PER-BOT: state/<cookie>.state.json — mỗi bot 1 file.

    Đường đọc KHÔNG lock (atomic write); chỉ lock khi cần migrate/ghi lần đầu
    → check_daily_quota trước mỗi claim không còn tốn flock.
    """
    path = _bot_state_path(cookie_file)
    entry, dirty = _load_entry(path, cookie_file)
    if not dirty:
        return entry
    os.makedirs(STATE_DIR, exist_ok=True)
    with _file_lock(path):
        entry, dirty = _load_entry(path, cookie_file)   # re-read dưới lock
        if dirty:
            _atomic_write_json(path, entry)
    return entry


def _save_state(state_path: str, states: dict):
    """Giữ tên cũ để tương thích — ghi atomic (dùng cho tool/migration bên ngoài)."""
    _atomic_write_json(state_path, states)


def save_daily_state(cookie_file: str, state: dict):
    """Ghi state vào FILE PER-BOT — không đụng bot nào khác."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = _bot_state_path(cookie_file)
    with _file_lock(path):
        _atomic_write_json(path, state)


def _update_state(cookie_file: str, mutate) -> dict:
    """Read-modify-write NGUYÊN TỬ trên FILE PER-BOT.

    Lock chỉ trong phạm vi 1 bot → 100 bot chạy song song KHÔNG chờ nhau."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = _bot_state_path(cookie_file)
    with _file_lock(path):
        entry, _ = _load_entry(path, cookie_file)
        entry = dict(entry)
        mutate(entry)
        _atomic_write_json(path, entry)
    return entry


def check_daily_quota(cookie_file: str,
                      max_claims=DEFAULT_MAX_CLAIMS_PER_DAY,
                      max_transfers=DEFAULT_MAX_TRANSFERS_PER_DAY,
                      max_amount=DEFAULT_MAX_TRANSFER_AMOUNT) -> tuple:
    """Trả về (ok, reason). ok=False nghĩa là đã vượt quota hôm nay."""
    st = load_daily_state(cookie_file)
    claims = int(st.get("claims_today", 0))
    transfers = int(st.get("transfers_today", 0))
    amount = int(st.get("amount_transferred", 0))
    if claims >= max_claims:
        return False, f"đã claim {claims}/{max_claims} hôm nay"
    if transfers >= max_transfers:
        return False, f"đã transfer {transfers}/{max_transfers} lần hôm nay"
    if amount >= max_amount:
        return False, f"đã transfer {amount:,}/{max_amount:,} xu hôm nay"
    ban_until = int(st.get("soft_ban_until", 0))
    if ban_until > time.time():
        return False, f"đang bị soft-ban, còn {int(ban_until - time.time())}s chờ"
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

    escalate=True: mỗi lần soft-ban liên tiếp nhân 1.6 lần thời gian chờ
    (tối đa 4 giờ) — FB thấy mình 'biết điều' thay vì retry đều đều."""
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


# ---- Giám sát 100+ bot: quét thư mục, KHÔNG cần file gộp, KHÔNG lock ----
def load_all_states() -> dict:
    """Trả về {cookie_file: state} của TẤT CẢ bot (quét state/*.state.json)."""
    out = {}
    legacy = _read_json(_STATE_FILE, {})        # file gộp cũ (nếu còn)
    if isinstance(legacy, dict):
        for k, v in legacy.items():
            if not k.startswith("_") and isinstance(v, dict):
                out[k] = v
    try:
        names = sorted(os.listdir(STATE_DIR))
    except Exception:
        names = []
    suffix = ".state.json"
    for name in names:
        if not name.endswith(suffix) or name.endswith(".lock"):
            continue
        st = _read_json(os.path.join(STATE_DIR, name), None)
        if isinstance(st, dict):
            out[name[:-len(suffix)]] = st
    return out


def summarize_states() -> dict:
    """Tổng hợp nhanh toàn bộ bot: tổng claim/transfer/xu, số bot đang soft-ban."""
    states = load_all_states()
    now = time.time()
    return {
        "bots":               len(states),
        "claims_today":       sum(int(s.get("claims_today", 0)) for s in states.values()),
        "transfers_today":    sum(int(s.get("transfers_today", 0)) for s in states.values()),
        "amount_transferred": sum(int(s.get("amount_transferred", 0)) for s in states.values()),
        "soft_banned_now":    sum(1 for s in states.values() if s.get("soft_ban_until", 0) > now),
    }


# =====================================================================
# E) SOFT-BAN / RATE-LIMIT DETECTION
# =====================================================================
# Regex có ranh giới từ — "blocked" trong link/CSS không match bừa.
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
    """Trả về pattern soft-ban match đầu tiên, hoặc None.

    Chống FALSE POSITIVE:
      - text ngắn (<1500 ký tự): 1 match là đủ (thường là thông báo lỗi).
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
    """Load danh sách dest_id từ transfer_dests.json. Fallback [default_id] nếu lỗi.

    Mỗi phần tử convert riêng: 1 entry hỏng không làm mất các dest còn lại."""
    if not os.path.exists(DEST_FILE):
        return [default_id]
    try:
        with open(DEST_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        dests = data.get("dests") or data.get("destinations") or []
        out = []
        for d in dests:
            try:
                v = int(d)
            except (TypeError, ValueError):
                continue
            if v > 0:
                out.append(v)
        return out or [default_id]
    except Exception:
        return [default_id]


def pick_next_dest(cookie_file: str, default_id: int = 51977054) -> int:
    """Xoay vòng dest_id per-cookie: cookie N luôn đi về dest[N % len(dests)].

    Nếu nhiều nick cùng transfer về 1 dest thì đó là hub-and-spoke pattern
    dễ bị Facebook phát hiện (graph anomaly) → xoay vòng giúp phân tán.
    Dùng md5 (không dùng hash()) — index ổn định giữa các process/lần chạy.
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
    """Args cho Chromium launch. Nhận optional fp dict: thêm --lang / --window-size
    khớp fingerprint (gọi không tham số vẫn hoạt động như cũ)."""
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
    """Mỗi ~`every` claim (± jitter), giả lập người dùng nghỉ 10-30s (lướt FB feed).

    Tránh pattern "claim liên tiếp nhiều giờ không nghỉ" — server sẽ flag.
    Ngưỡng nghỉ jitter → không phải lúc nào cũng đúng % 60.
    Tổng thời gian nghỉ (đã trừ phần cuộn feed) = IDLE_WAIT_MIN..IDLE_WAIT_MAX giây.
    """
    if max_claims_so_far <= 0:
        return
    every_now = max(10, every + random.randint(-10, 15))
    if max_claims_so_far % every_now != 0:
        return
    total = random.uniform(IDLE_WAIT_MIN, IDLE_WAIT_MAX)
    print(f"  😴 Đã claim {max_claims_so_far} lần — nghỉ {total:.0f}s giả lập người dùng.",
          flush=True)
    # Optionally scroll để giống người lướt feed (ăn một phần thời gian nghỉ)
    t0 = time.time()
    try:
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(100, 400))
            time.sleep(random.uniform(0.8, 2.5))
    except Exception:
        pass
    remain = total - (time.time() - t0)
    if remain > 0:
        time.sleep(remain)


def warm_up_account(page, fb_url: str = "https://www.facebook.com/", max_s: int = 15):
    """Sau khi login, scroll FB feed ~max_s giây trước khi vào game — giống người thật.

    Tôn trọng ngân sách `max_s`. Trả về tổng số giây đã warm-up.
    """
    deadline = time.time() + max(1.0, float(max_s))
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
    print("=== anti_lock.py self-test ===")
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
    print("\n--- Per-bot sharding ---")
    print("  fp path   :", _fp_path("ck_test.txt"))
    print("  state path:", _bot_state_path("ck_test.txt"))
    record_claim("ck_test.txt")
    print("  load_daily_state:", load_daily_state("ck_test.txt"))
    print("  summarize_states:", summarize_states())
    print("  load_all_states keys:", list(load_all_states().keys()))
    print("  bot_concurrency:", bot_concurrency())
    print("\n=== OK ===")
