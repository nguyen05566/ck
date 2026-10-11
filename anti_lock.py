#!/usr/bin/env python3
"""

  A) Fingerprint ngẫu nhiên NHƯNG cố định per-account:
     Mỗi cookie file (ck1.txt, ck2.txt ...) sẽ luôn dùng cùng 1 UA / viewport /
     locale / timezone / WebGL vendor — không đổi giữa các lần chạy → giống 1
     thiết bị thật, nhưng khác nhau giữa các nick → không bị cluster bot-farm.

  B) Stealth JS injection:
     Xóa navigator.webdriver, fake plugins, fake chrome.runtime, override
     permissions.query, spoof WebGL vendor/renderer, fake hardwareConcurrency.
     (Playwright's launch flag `--disable-blink-features=AutomationControlled`
     không đủ — Facebook còn check nhiều hook khác.)

  C) Human-like timing:
     - human_delay(min,max)   → phân phối Poisson thay vì fixed 3.0s
     - jitter_sleep(base,jit) → base + random jitter
     - random_batch_size(n)   → batch size biến thiên thay vì luôn 40

  D) Daily quota / state file:
     Theo dõi số claim / số transfer / số xu đã transfer mỗi ngày cho mỗi cookie.
     Vượt MAX_CLAIMS_PER_DAY → tự dừng, tránh spam server.

  E) Soft-ban detection:
     Phát hiện keyword "rate limit" / "too many requests" / "tạm thời" / "khóa"
     trong response → back-off dài (15-30 phút) trước khi thử lại.

  F) Transfer destination rotation:
     Đọc danh sách hub account từ transfer_dests.json → xoay vòng, không phải
     luôn gửi về cùng 1 dest_id (tránh hub-and-spoke detection).

  G) Random session offset:
     Sleep 0-180s ngẫu nhiên trước khi mở session đầu tiên → 8 nick không
     "đồng loạt" bật browser cùng 1 lúc.
"""
import os
import json
import time
import random

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


def get_or_create_fingerprint(cookie_file: str) -> dict:
    """Trả về fingerprint cố định cho 1 cookie file. Tự sinh + lưu nếu chưa có."""
    os.makedirs(FP_DIR, exist_ok=True)
    fp_path = os.path.join(FP_DIR, "fingerprint.json")

    fingerprints = {}
    if os.path.exists(fp_path):
        try:
            with open(fp_path, "r", encoding="utf-8") as fh:
                fingerprints = json.load(fh)
        except Exception:
            fingerprints = {}

    key = os.path.basename(cookie_file)
    if key not in fingerprints:
        ua = random.choice(USER_AGENTS)
        vendor, renderer = random.choice(WEBGL_VENDORS)
        fingerprints[key] = {
            "user_agent":   ua,
            "viewport":     random.choice(VIEWPORTS),
            "locale":       random.choice(LOCALES),
            "timezone":     random.choice(TIMEZONES),
            "platform":     _platform_for(ua),
            "webgl_vendor": vendor,
            "webgl_renderer": renderer,
            "hardware_concurrency": random.choice([4, 8, 8, 12, 16]),
            "device_memory": random.choice([4, 8, 8, 16]),
            "created_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            with open(fp_path, "w", encoding="utf-8") as fh:
                json.dump(fingerprints, fh, indent=2, ensure_ascii=False)
        except Exception:
            pass

    return fingerprints[key]


# =====================================================================
# B) STEALTH JS — inject vào mọi frame trước khi page load
# =====================================================================
def build_stealth_js(fp: dict) -> str:
    """Trả về JS string để add_init_script(). Nhận fingerprint dict."""
    return f"""() => {{
    try {{
        // 1. navigator.webdriver = undefined
        Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
        delete navigator.__proto__.webdriver;

        // 2. navigator.platform khớp với UA
        Object.defineProperty(navigator, 'platform', {{get: () => '{fp["platform"]}'}});

        // 3. navigator.plugins — Chrome thật có 3 plugin mặc định
        const fakePlugins = [
            {{name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
            {{name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''}},
            {{name: 'Native Client', filename: 'internal-nacl-plugin', description: ''}}
        ];
        Object.defineProperty(navigator, 'plugins', {{
            get: () => fakePlugins,
            configurable: true
        }});

        // 4. navigator.languages
        Object.defineProperty(navigator, 'languages', {{get: () => ['{fp["locale"]}', 'en']}});

        // 5. navigator.hardwareConcurrency / deviceMemory
        Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {fp["hardware_concurrency"]}}});
        Object.defineProperty(navigator, 'deviceMemory', {{get: () => {fp["device_memory"]}}});

        // 6. window.chrome runtime (Playwright sẽ không có sẵn)
        if (!window.chrome) {{
            window.chrome = {{runtime: {{}}, app: {{}}, csi: () => {{}}, loadTimes: () => {{}}}};
        }}

        // 7. Override permissions.query để trả về Notification.permission
        const origQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (origQuery) {{
            window.navigator.permissions.query = (parameters) => {{
                if (parameters && parameters.name === 'notifications') {{
                    return Promise.resolve({{state: Notification.permission}});
                }}
                return origQuery.call(window.navigator.permissions, parameters);
            }};
            // patch toString để không bị phát hiện qua Function.prototype.toString
            const origToString = Function.prototype.toString;
            window.navigator.permissions.query.toString = () => origToString.call(origQuery);
            Function.prototype.toString = function() {{
                if (this === window.navigator.permissions.query) return 'function query() {{ [native code] }}';
                return origToString.call(this);
            }};
        }}

        // 8. WebGL fingerprint spoofing
        const getParameterProto = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {{
            // UNMASKED_VENDOR_WEBGL = 37445, UNMASKED_RENDERER_WEBGL = 37446
            if (parameter === 37445) return '{fp["webgl_vendor"]}';
            if (parameter === 37446) return '{fp["webgl_renderer"]}';
            return getParameterProto.call(this, parameter);
        }};
        if (window.WebGL2RenderingContext) {{
            const getParameterProto2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(parameter) {{
                if (parameter === 37445) return '{fp["webgl_vendor"]}';
                if (parameter === 37446) return '{fp["webgl_renderer"]}';
                return getParameterProto2.call(this, parameter);
            }};
        }}

        // 9. Hide iframe contentWindowwebdriver
        try {{
            const originalAttachShadow = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function() {{ return originalShadowRef.apply(this, arguments); }};
            const originalShadowRef = originalAttachShadow;
        }} catch (e) {{}}

        // 10. Hide WebRTC IP leak (để proxy không bị leak IP thật)
        if (window.RTCPeerConnection) {{
            const origRTC = window.RTCPeerConnection;
            window.RTCPeerConnection = function(...args) {{
                if (args[0] && args[0].iceServers) {{
                    args[0].iceServers = [];
                }}
                return new origRTC(...args);
            }};
            window.RTCPeerConnection.prototype = origRTC.prototype;
        }}
    }} catch (e) {{
        // stealth fail silent — không crash game
    }}
}}"""


# =====================================================================
# C) HUMAN-LIKE TIMING
# =====================================================================
def human_delay(min_s: float = 2.0, max_s: float = 6.0):
    """Sleep với phân phối Poisson (giống người — lâu lâu mới có cú click nhanh)."""
    mean = (min_s + max_s) / 2
    delay = -1.0
    tries = 0
    while (delay < min_s or delay > max_s) and tries < 10:
        delay = random.expovariate(1.0 / mean) if mean > 0 else min_s
        tries += 1
    if delay < min_s:
        delay = min_s
    elif delay > max_s:
        delay = max_s
    time.sleep(delay)


def jitter_sleep(base_s: float, jitter_s: float = 0.6):
    """Base delay + jitter ngẫu nhiên. Dùng cho các chờ nhỏ giữa claim."""
    time.sleep(max(0.1, base_s + random.uniform(0, jitter_s)))


def random_batch_size(base: int = 40, lo: int = 25, hi: int = 55) -> int:
    """Trả về batch size ngẫu nhiên quanh `base`, tránh pattern fixed-40."""
    return max(5, random.randint(lo, hi))


def random_session_offset(max_s: int = 180) -> float:
    """Sleep 0..max_s giây trước khi mở session đầu tiên."""
    wait = random.uniform(0, max_s)
    time.sleep(wait)
    return wait


# =====================================================================
# D) DAILY QUOTA / STATE
# =====================================================================
DEFAULT_MAX_CLAIMS_PER_DAY    = 180   # ~3 batch x 40 + buffer
DEFAULT_MAX_TRANSFERS_PER_DAY = 30
DEFAULT_MAX_TRANSFER_AMOUNT   = 2_000_000  # tổng xu transfer/ngày/cookie

def _today() -> str:
    return time.strftime("%Y-%m-%d")


def load_daily_state(cookie_file: str) -> dict:
    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, "daily_state.json")

    states = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                states = json.load(fh)
        except Exception:
            states = {}

    key = os.path.basename(cookie_file)
    today = _today()
    if key not in states or states[key].get("date") != today:
        states[key] = {
            "date":               today,
            "claims_today":       0,
            "transfers_today":    0,
            "amount_transferred": 0,
            "sessions_today":     0,
            "last_run_ts":        0,
            "soft_ban_until":     0,    # epoch seconds — nếu > now thì đang bị soft-ban
        }
        _save_state(state_path, states)
    return states[key]


def _save_state(state_path: str, states: dict):
    try:
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(states, fh, indent=2, ensure_ascii=False)
    except Exception:
        pass


def save_daily_state(cookie_file: str, state: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, "daily_state.json")
    states = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                states = json.load(fh)
        except Exception:
            states = {}
    states[os.path.basename(cookie_file)] = state
    _save_state(state_path, states)


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
    st = load_daily_state(cookie_file)
    st["claims_today"] = st.get("claims_today", 0) + 1
    save_daily_state(cookie_file, st)


def record_transfer(cookie_file: str, amount: int = 0):
    st = load_daily_state(cookie_file)
    st["transfers_today"] = st.get("transfers_today", 0) + 1
    st["amount_transferred"] = st.get("amount_transferred", 0) + amount
    save_daily_state(cookie_file, st)


def record_session_start(cookie_file: str):
    st = load_daily_state(cookie_file)
    st["sessions_today"] = st.get("sessions_today", 0) + 1
    st["last_run_ts"] = int(time.time())
    save_daily_state(cookie_file, st)


def set_soft_ban(cookie_file: str, duration_s: int = 900):
    """Đặt cooldown soft-ban (mặc định 15 phút)."""
    st = load_daily_state(cookie_file)
    st["soft_ban_until"] = int(time.time()) + duration_s
    save_daily_state(cookie_file, st)


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


def detect_soft_ban(text: str) -> bool:
    if not text:
        return False
    t = str(text).lower()
    return any(h in t for h in SOFT_BAN_HINTS)


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
    """
    dests = load_transfer_dests(default_id)
    if len(dests) == 1:
        return dests[0]
    # Hash cookie_file → index cố định, không đổi giữa các lần chạy
    key = os.path.basename(cookie_file)
    idx = abs(hash(key)) % len(dests)
    # Thêm jitter: đôi khi chọn dest kế bên để trông tự nhiên hơn
    if random.random() < 0.15:
        idx = (idx + 1) % len(dests)
    return dests[idx]


# =====================================================================
# G) BROWSER LAUNCH ARGS (chống detect + chống leak)
# =====================================================================
def stealth_launch_args() -> list:
    """Args cho Chromium launch — mạnh hơn --disable-blink-features."""
    return [
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


# =====================================================================
# H) HUMAN-LIKE IDLE — tạo "session nghỉ" ngắn giữa các batch
# =====================================================================
def maybe_idle_browse(page, max_claims_so_far: int, every: int = 60):
    """Mỗi `every` claim, giả lập người dùng nghỉ 30-90s (lướt FB feed).

    Tránh pattern "claim liên tục 5.5 giờ không nghỉ" — server sẽ flag.
    """
    if max_claims_so_far <= 0 or max_claims_so_far % every != 0:
        return
    wait = random.uniform(30, 90)
    print(f"  😴 Đã claim {max_claims_so_far} lần — nghỉ {wait:.0f}s giả lập người dùng.",
          flush=True)
    # Optionally scroll để giống người lướt feed
    try:
        page.mouse.wheel(0, random.randint(100, 400))
    except Exception:
        pass
    time.sleep(wait)


def warm_up_account(page, fb_url: str = "https://www.facebook.com/", max_s: int = 15):
    """Sau khi login, scroll FB feed 5-15s trước khi vào game — giống người thật."""
    try:
        page.mouse.wheel(0, random.randint(200, 600))
        time.sleep(random.uniform(2, 5))
        page.mouse.wheel(0, random.randint(300, 800))
        time.sleep(random.uniform(2, 5))
        # Thỉnh thoảng hover 1 element
        try:
            page.mouse.move(random.randint(200, 1500), random.randint(200, 800))
            time.sleep(random.uniform(1, 3))
        except Exception:
            pass
    except Exception:
        # Ignore — không phải lỗi nghiêm trọng
        pass


# =====================================================================
# Self-test khi chạy trực tiếp
# =====================================================================
if __name__ == "__main__":
    print("=== anti_lock.py self-test ===")
    fp = get_or_create_fingerprint("ck_test.txt")
    print("Fingerprint for ck_test.txt:")
    print(json.dumps(fp, indent=2, ensure_ascii=False))
    print("\nStealth JS preview (first 200 chars):")
    print(build_stealth_js(fp)[:200], "...")
    print("\nDaily quota check:")
    ok, reason = check_daily_quota("ck_test.txt")
    print(f"  ok={ok}  reason={reason}")
    print(f"\nTransfer dests loaded: {load_transfer_dests()}")
    print(f"Next dest for ck_test.txt: {pick_next_dest('ck_test.txt')}")
    print(f"Launch args count: {len(stealth_launch_args())}")
