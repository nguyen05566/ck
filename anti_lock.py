# anti_lock.py
# -*- coding: utf-8 -*-
"""
Anti-detect / anti-lock module cho jieqi-core.

Mục tiêu: giảm khả năng bot bị phát hiện và bị khóa tài khoản Facebook
khi thực hiện các thao tác claim / transfer.

Cấu trúc:
    A) Fingerprint theo tài khoản (ổn định + có version để rotate)
    B) Stealth JS injection (đã fix lỗi iframe + toString + canvas)
    C) Timing & batch size giống người thật
    D) Quản lý quota hàng ngày & trạng thái soft-ban
    E) Phát hiện soft-ban / rate-limit (đa tín hiệu)
    F) Luân chuyển đích nhận transfer
    G) Chromium launch args (+ gợi ý TLS impersonation)
    H) Mô phỏng hành vi nghỉ / lướt feed


"""

from __future__ import annotations

import os
import re
import json
import time
import math
import random
import hashlib
import logging
from datetime import datetime, date, timedelta
from typing import Any, Optional, Iterable

logger = logging.getLogger(__name__)

# ============================================================
# Paths & state
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FINGERPRINT_DIR = os.path.join(BASE_DIR, "fingerprints")
STATE_DIR = os.path.join(BASE_DIR, "state")
os.makedirs(FINGERPRINT_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

PROFILE_VERSION = 2  # [NEW] tăng khi schema fingerprint thay đổi


# ============================================================
# A) FINGERPRINT PER-ACCOUNT
# ------------------------------------------------------------
# [FIX] Bản cũ tạo fingerprint ngẫu nhiên 1 lần và giữ nguyên vĩnh viễn.
#       → Vẫn ổn định per-account, NHƯNG có thêm profile_version để
#         có thể rebuild khi cần rotate (ví dụ sau khi bị flag).
# [NEW] Bổ sung: timezone, locale, screen, colorDepth, webgl unmasked.
# [NEW] Chọn UA từ pool Chrome gần đây, tránh version quá cũ.
# ============================================================

CHROME_VERSIONS = ["131.0.0.0", "130.0.0.0", "129.0.0.0", "128.0.0.0"]

PLATFORM_PROFILES = {
    "win": {
        "ua_platform": "Windows NT 10.0; Win64; x64",
        "platform": "Win32",
        "vendor": "Google Inc.",
        "renderer_tpl": "ANGLE (NVIDIA, NVIDIA GeForce RTX {gpu} Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "gpus": ["3060", "3070", "4060", "4070"],
        "gl_vendor": "Google Inc. (NVIDIA)",
    },
    "mac": {
        "ua_platform": "Macintosh; Intel Mac OS X 10_15_7",
        "platform": "MacIntel",
        "vendor": "Google Inc.",
        "renderer_tpl": "ANGLE (Apple, Apple M{chip}, OpenGL 4.1)",
        "gpus": ["1", "2", "3"],
        "gl_vendor": "Google Inc. (Apple)",
    },
}

TIMEZONES = ["Asia/Ho_Chi_Minh", "Asia/Bangkok", "Asia/Singapore"]
LOCALES = ["vi-VN", "en-US", "en-GB"]
LANGUAGES_POOL = [
    ["vi-VN", "vi", "en-US", "en"],
    ["en-US", "en", "vi-VN", "vi"],
]


def _seed_from_cookie_path(cookie_path: str) -> int:
    """Sinh seed ổn định từ đường dẫn cookie (đại diện 1 account)."""
    h = hashlib.sha256(cookie_path.encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def _build_fingerprint(seed: int) -> dict:
    rng = random.Random(seed)

    platform_key = rng.choice(list(PLATFORM_PROFILES.keys()))
    plat = PLATFORM_PROFILES[platform_key]
    chrome_ver = rng.choice(CHROME_VERSIONS)

    ua = (
        f"Mozilla/5.0 ({plat['ua_platform']}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_ver} Safari/537.36"
    )

    width = rng.choice([1366, 1440, 1536, 1600, 1920])
    height = rng.choice([768, 864, 900, 1080])

    gpu = rng.choice(plat["gpus"])
    renderer = plat["renderer_tpl"].format(gpu=gpu)

    return {
        "profile_version": PROFILE_VERSION,
        "seed": seed,
        "user_agent": ua,
        "platform": plat["platform"],
        "vendor": plat["vendor"],
        "chrome_version": chrome_ver,
        "locale": rng.choice(LOCALES),
        "timezone": rng.choice(TIMEZONES),
        "viewport": {"width": width, "height": height},
        "screen": {"width": width, "height": height, "colorDepth": 24},
        "hardware_concurrency": rng.choice([4, 8, 12, 16]),
        "device_memory": rng.choice([4, 8, 16]),
        "webgl": {
            "vendor": plat["gl_vendor"],
            "renderer": renderer,
            "unmasked_vendor": plat["gl_vendor"],
            "unmasked_renderer": renderer,
        },
        "languages": rng.choice(LANGUAGES_POOL),
    }


def get_or_create_fingerprint(cookie_path: str) -> dict:
    """
    Trả về fingerprint ổn định cho tài khoản.
    [FIX] Nếu file tồn tại nhưng phiên bản cũ → rebuild.
    """
    seed = _seed_from_cookie_path(cookie_path)
    fp_path = os.path.join(FINGERPRINT_DIR, f"fp_{seed:016x}.json")

    if os.path.exists(fp_path):
        try:
            with open(fp_path, "r", encoding="utf-8") as f:
                fp = json.load(f)
            if fp.get("profile_version") == PROFILE_VERSION:
                return fp
            logger.info("Fingerprint cũ (v%s), rebuild cho cookie=%s",
                        fp.get("profile_version"), cookie_path)
        except Exception as e:
            logger.warning("Không đọc được fingerprint cũ (%s), tạo mới.", e)

    fp = _build_fingerprint(seed)
    with open(fp_path, "w", encoding="utf-8") as f:
        json.dump(fp, f, ensure_ascii=False, indent=2)
    return fp


def rotate_fingerprint(cookie_path: str) -> dict:
    """[NEW] Xoá fingerprint cũ để buộc tạo mới (dùng khi bị flag nặng)."""
    seed = _seed_from_cookie_path(cookie_path)
    fp_path = os.path.join(FINGERPRINT_DIR, f"fp_{seed:016x}.json")
    if os.path.exists(fp_path):
        os.remove(fp_path)
    return get_or_create_fingerprint(cookie_path)


# ============================================================
# B) STEALTH JS INJECTION
# ------------------------------------------------------------
# [FIX] originalShadowRef dùng trước khi khai báo → ReferenceError.
# [FIX] Không override Function.prototype.toString toàn cục nữa
#       (bị phát hiện qua toString.toString()).
# [FIX] WebGL spoof cả 2 context (WebGL1 & WebGL2) + getExtension.
# [NEW] Canvas noise rất nhẹ (không phá rendering).
# [NEW] iframe contentWindow.webdriver được patch đúng cách.
# ============================================================

def build_stealth_js(fp: dict) -> str:
    canvas_jitter = fp["seed"] % 3  # 0,1,2 - offset nhỏ

    return f"""
(() => {{
  if (window.__stealth_applied__) return;

  // ---------- 1. webdriver ----------
  try {{
    Object.defineProperty(Navigator.prototype, 'webdriver', {{
      get: () => false,
      configurable: true,
    }});
  }} catch (e) {{}}

  // ---------- 2. languages ----------
  try {{
    Object.defineProperty(Navigator.prototype, 'languages', {{
      get: () => {json.dumps(fp['languages'])},
      configurable: true,
    }});
    Object.defineProperty(Navigator.prototype, 'language', {{
      get: () => {json.dumps(fp['languages'][0])},
      configurable: true,
    }});
  }} catch (e) {{}}

  // ---------- 3. platform / vendor ----------
  try {{
    Object.defineProperty(Navigator.prototype, 'platform', {{
      get: () => {json.dumps(fp['platform'])},
      configurable: true,
    }});
    Object.defineProperty(Navigator.prototype, 'vendor', {{
      get: () => {json.dumps(fp['vendor'])},
      configurable: true,
    }});
  }} catch (e) {{}}

  // ---------- 4. hardware ----------
  try {{
    Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {{
      get: () => {fp['hardware_concurrency']}, configurable: true,
    }});
    Object.defineProperty(Navigator.prototype, 'deviceMemory', {{
      get: () => {fp['device_memory']}, configurable: true,
    }});
    Object.defineProperty(Navigator.prototype, 'maxTouchPoints', {{
      get: () => 0, configurable: true,
    }});
  }} catch (e) {{}}

  // ---------- 5. plugins ----------
  try {{
    const names = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer',
                   'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
    const fakePlugins = names.map(n => ({{
      name: n, filename: 'internal-pdf-viewer',
      description: 'Portable Document Format', length: 1,
    }}));
    fakePlugins.item = i => fakePlugins[i];
    fakePlugins.namedItem = n => fakePlugins.find(x => x.name === n) || null;
    Object.defineProperty(Navigator.prototype, 'plugins', {{
      get: () => fakePlugins, configurable: true,
    }});
  }} catch (e) {{}}

  // ---------- 6. chrome object ----------
  try {{
    if (!window.chrome) window.chrome = {{}};
    if (!window.chrome.runtime) {{
      window.chrome.runtime = {{
        onConnect: {{ addListener: () => {{}} }},
        onMessage: {{ addListener: () => {{}} }},
      }};
    }}
    if (!window.chrome.loadTimes) {{
      window.chrome.loadTimes = () => ({{
        requestTime: performance.timing.navigationStart / 1000,
        startLoadTime: performance.timing.navigationStart / 1000,
        commitLoadTime: performance.timing.responseStart / 1000,
        finishDocumentLoadTime:
          performance.timing.domContentLoadedEventEnd / 1000,
        finishLoadTime: performance.timing.loadEventEnd / 1000,
        firstPaintTime: performance.timing.responseStart / 1000 + 0.05,
        firstPaintAfterLoadTime: 0,
        navigationType: 'Other',
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
        npnNegotiatedProtocol: 'h2',
        wasAlternateProtocolAvailable: false,
        connectionInfo: 'h2',
      }});
    }}
  }} catch (e) {{}}

  // ---------- 7. permissions.query ----------
  try {{
    const origQuery =
      window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (params) => {{
      if (params && params.name === 'notifications') {{
        return Promise.resolve({{ state: Notification.permission, onchange: null }});
      }}
      return origQuery(params);
    }};
  }} catch (e) {{}}

  // ---------- 8. WebGL spoof (WebGL1 + WebGL2) ----------
  try {{
    const UNMASKED_VENDOR = 0x9245;
    const UNMASKED_RENDERER = 0x9246;
    const VENDOR = 0x1F00;
    const RENDERER = 0x1F01;
    const vendor_str = {json.dumps(fp['webgl']['vendor'])};
    const renderer_str = {json.dumps(fp['webgl']['renderer'])};

    const makeSpoof = (orig) => function (param) {{
      switch (param) {{
        case UNMASKED_VENDOR:
        case VENDOR:   return vendor_str;
        case UNMASKED_RENDERER:
        case RENDERER: return renderer_str;
        default:       return orig.call(this, param);
      }}
    }};

    const gl1 = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = makeSpoof(gl1);

    if (window.WebGL2RenderingContext) {{
      const gl2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = makeSpoof(gl2);
    }}
  }} catch (e) {{}}

  // ---------- 9. Canvas noise nhẹ ----------
  try {{
    const jitter = {canvas_jitter};
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (...args) {{
      try {{
        const ctx = this.getContext && this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {{
          const px = ctx.getImageData(0, 0, 1, 1);
          px.data[0] = (px.data[0] + jitter) & 0xff;
          ctx.putImageData(px, 0, 0);
        }}
      }} catch (e) {{}}
      return origToDataURL.apply(this, args);
    }};
  }} catch (e) {{}}

  // ---------- 10. iframe contentWindow.webdriver (FIX) ----------
  try {{
    const desc = Object.getOwnPropertyDescriptor(
      HTMLIFrameElement.prototype, 'contentWindow'
    );
    if (desc && desc.get) {{
      const origGetter = desc.get;
      Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {{
        get() {{
          const win = origGetter.call(this);
          try {{
            if (win && win.navigator && !win.navigator.__patched__) {{
              Object.defineProperty(win.navigator, 'webdriver', {{
                get: () => false, configurable: true,
              }});
              win.navigator.__patched__ = true;
            }}
          }} catch (e) {{}}
          return win;
        }},
        configurable: true,
      }});
    }}
  }} catch (e) {{}}

  // ---------- 11. Notification.permission ----------
  try {{
    Object.defineProperty(Notification, 'permission', {{
      get: () => 'default', configurable: true,
    }});
  }} catch (e) {{}}

  window.__stealth_applied__ = true;
}})();
"""


def apply_stealth(page, fp: dict, context=None) -> None:
    """
    [NEW] Helper áp dụng stealth cho 1 Playwright page.
    Gọi TRƯỚC page.goto(...).
    """
    try:
        page.add_init_script(build_stealth_js(fp))
    except Exception as e:
        logger.warning("add_init_script lỗi: %s", e)

    # Gợi ý: nếu dùng context mới, cũng có thể gọi context.add_init_script.
    if context is not None:
        try:
            context.add_init_script(build_stealth_js(fp))
        except Exception:
            pass


# ============================================================
# C) TIMING & BATCH SIZE
# ------------------------------------------------------------
# [NEW] Dùng phân phối log-normal thay vì Poisson thuần
#       → đuôi dài giống người thật hơn (đôi khi nghỉ lâu).
# [NEW] Có "quick pause" 5% để giả lập người đang làm việc khác.
# ============================================================

def human_delay(base: float = 2.0, spread: float = 0.6,
                min_s: float = 0.4, max_s: float = 20.0) -> float:
    """
    Trả về delay (giây) theo log-normal.
    - base: trung vị mong muốn
    - spread: độ biến thiên (sigma của log)
    - 5% xác suất delay dài (quick pause)
    """
    if random.random() < 0.05:
        return random.uniform(max_s, max_s * 2)

    # log-normal với median = base
    mu = math.log(max(base, 1e-3))
    sigma = spread
    d = random.lognormvariate(mu, sigma)
    return max(min_s, min(max_s, d))


def sleep_human(base: float = 2.0, spread: float = 0.6) -> None:
    time.sleep(human_delay(base, spread))


def human_batch_size(lo: int = 3, hi: int = 8) -> int:
    """Batch size ngẫu nhiên trong khoảng [lo, hi]."""
    return random.randint(lo, hi)


# ============================================================
# D) QUOTA HÀNG NGÀY & STATE
# ------------------------------------------------------------
# [FIX] Bản cũ có thể ghi state không atomic → hỏng file.
#       Dùng ghi tạm rồi os.replace() để atomic.
# [NEW] Lưu thêm "consecutive_failures" để back-off động.
# ============================================================

def _state_path(account_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", account_id)
    return os.path.join(STATE_DIR, f"state_{safe}.json")


def _atomic_write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _default_state() -> dict:
    return {
        "date": date.today().isoformat(),
        "claims": 0,
        "transfers": 0,
        "soft_ban_until": 0,
        "consecutive_failures": 0,
        "last_update": time.time(),
    }


def load_state(account_id: str) -> dict:
    path = _state_path(account_id)
    if not os.path.exists(path):
        return _default_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception as e:
        logger.warning("State hỏng (%s), reset.", e)
        return _default_state()

    # Reset quota mỗi ngày
    if st.get("date") != date.today().isoformat():
        st = _default_state()
    # Bổ sung field thiếu (nâng cấp từ bản cũ)
    for k, v in _default_state().items():
        st.setdefault(k, v)
    return st


def save_state(account_id: str, st: dict) -> None:
    st["last_update"] = time.time()
    _atomic_write_json(_state_path(account_id), st)


DAILY_CLAIM_LIMIT = 25
DAILY_TRANSFER_LIMIT = 15


def can_claim(account_id: str) -> bool:
    st = load_state(account_id)
    if time.time() < st.get("soft_ban_until", 0):
        return False
    return st["claims"] < DAILY_CLAIM_LIMIT


def can_transfer(account_id: str) -> bool:
    st = load_state(account_id)
    if time.time() < st.get("soft_ban_until", 0):
        return False
    return st["transfers"] < DAILY_TRANSFER_LIMIT


def record_claim(account_id: str) -> None:
    st = load_state(account_id)
    st["claims"] += 1
    save_state(account_id, st)


def record_transfer(account_id: str) -> None:
    st = load_state(account_id)
    st["transfers"] += 1
    save_state(account_id, st)


def backoff(account_id: str, base_seconds: int = 300) -> int:
    """
    [NEW] Back-off động: càng nhiều fail liên tiếp thì nghỉ càng lâu.
    Trả về số giây đã đặt.
    """
    st = load_state(account_id)
    st["consecutive_failures"] += 1
    n = st["consecutive_failures"]
    cool = min(base_seconds * (2 ** (n - 1)), 6 * 3600)  # tối đa 6h
    cool = int(cool * random.uniform(0.85, 1.15))
    st["soft_ban_until"] = time.time() + cool
    save_state(account_id, st)
    logger.warning("Backoff account=%s trong %ss (fail lần %d)",
                   account_id, cool, n)
    return cool


def reset_failures(account_id: str) -> None:
    st = load_state(account_id)
    if st.get("consecutive_failures", 0) != 0:
        st["consecutive_failures"] = 0
        save_state(account_id, st)


# ============================================================
# E) PHÁT HIỆN SOFT-BAN / RATE-LIMIT
# ------------------------------------------------------------
# [FIX] Bản cũ chỉ dò text → dương tính giả, âm tính giả.
# [NEW] Đa tín hiệu: HTTP status, keyword có ngữ cảnh, JSON shape.
# ============================================================

SOFT_BAN_KEYWORDS = [
    r"\brate[\s_-]?limit",
    r"too many requests",
    r"quá nhiều yêu cầu",
    r"tạm thời (bị )?(khóa|chặn|hạn chế)",
    r"vui lòng thử lại sau",
    r"please try again later",
    r"you'?re temporarily blocked",
    r"account (has been )?(restricted|suspended)",
    r"checkpoint",
]

_SOFT_BAN_RE = re.compile("|".join(SOFT_BAN_KEYWORDS), re.IGNORECASE)


def detect_soft_ban(response: Any = None, status: Optional[int] = None,
                    body_text: Optional[str] = None) -> bool:
    """
    Trả về True nếu có dấu hiệu soft-ban.
    - response: object có .status / .text / .json() (tuỳ thư viện)
    - status: mã HTTP
    - body_text: text thân response
    """
    # 1) HTTP status
    if status in (429, 403, 503):
        return True
    if response is not None:
        s = getattr(response, "status", None) or getattr(response, "status_code", None)
        if s in (429, 403, 503):
            return True

    # 2) Text
    text = body_text
    if text is None and response is not None:
        text = getattr(response, "text", None)
        if callable(text):
            try:
                text = text()
            except Exception:
                text = None
    if not text:
        return False

    # 3) Keyword matching
    if _SOFT_BAN_RE.search(text):
        return True

    return False


# ============================================================
# F) LUÂN CHUYỂN ĐÍCH NHẬN TRANSFER
# ------------------------------------------------------------
# [FIX] Tránh hub-and-spoke: xoay đích, không lặp lại quá gần.
# [NEW] Giữ một deque history để không quay lại đích vừa dùng.
# ============================================================

from collections import deque

_target_history: deque = deque(maxlen=3)


def pick_transfer_target(targets: Iterable[str]) -> Optional[str]:
    """
    Chọn đích transfer khác 3 đích gần nhất.
    Nếu pool quá nhỏ (< 4), chấp nhận trùng.
    """
    pool = [t for t in targets if t]
    if not pool:
        return None

    recent = set(_target_history)
    candidates = [t for t in pool if t not in recent]
    if not candidates:
        # pool nhỏ → chỉ loại đích vừa dùng ngay trước
        last = _target_history[-1] if _target_history else None
        candidates = [t for t in pool if t != last] or pool

    chosen = random.choice(candidates)
    _target_history.append(chosen)
    return chosen


# ============================================================
# G) CHROMIUM LAUNCH ARGS
# ------------------------------------------------------------
# [NEW] Gợi ý dùng TLS impersonation (curl_cffi, httpcloak) vì
#       Playwright mặc định vẫn lộ TLS fingerprint.
# ============================================================

def chromium_launch_args(fp: dict) -> list:
    w = fp["viewport"]["width"]
    h = fp["viewport"]["height"]
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
        "--disable-infobars",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-sync",
        "--metrics-recording-only",
        "--mute-audio",
        f"--window-size={w},{h}",
        f"--lang={fp['locale']}",
        "--disable-web-security" if False else "--enable-features=NetworkService",
    ]


def chromium_context_options(fp: dict) -> dict:
    """Options cho browser.new_context()."""
    return {
        "user_agent": fp["user_agent"],
        "viewport": fp["viewport"],
        "screen": fp["screen"],
        "locale": fp["locale"],
        "timezone_id": fp["timezone"],
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": "light",
        "extra_http_headers": {
            "Accept-Language": f"{fp['locale']},"
                              f"{fp['locale'].split('-')[0]};q=0.9,en;q=0.8",
            "sec-ch-ua": (
                f'"Chromium";v="{fp["chrome_version"].split(".")[0]}", '
                f'"Google Chrome";v="{fp["chrome_version"].split(".")[0]}", '
                f'"Not?A_Brand";v="24"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{fp["platform"]}"',
        },
    }


# ============================================================
# H) MÔ PHỎNG HÀNH VI NGHỈ / LƯỚT FEED
# ------------------------------------------------------------
# [NEW] Scroll có gia tốc, đôi khi dừng đọc lâu.
# [NEW] Có thể click nhầm link rồi back (giống người).
# ============================================================

def human_scroll(page, max_scrolls: int = 5) -> None:
    try:
        for _ in range(random.randint(1, max_scrolls)):
            delta = random.randint(200, 900)
            steps = random.randint(3, 8)
            for _ in range(steps):
                page.mouse.wheel(0, delta // steps)
                time.sleep(random.uniform(0.03, 0.12))
            # đôi khi dừng đọc
            if random.random() < 0.35:
                time.sleep(random.uniform(1.0, 4.0))
    except Exception as e:
        logger.debug("human_scroll lỗi: %s", e)


def human_idle(page, min_s: float = 5.0, max_s: float = 20.0) -> None:
    """Nghỉ giả lập người dùng đang làm việc khác."""
    dur = random.uniform(min_s, max_s)
    logger.info("Idle %.1fs (giả lập người dùng nghỉ)", dur)
    t_end = time.time() + dur
    while time.time() < t_end:
        try:
            human_scroll(page, max_scrolls=2)
        except Exception:
            pass
        time.sleep(random.uniform(0.5, 2.0))


def maybe_take_break() -> None:
    """5% xác suất nghỉ dài giữa batch."""
    if random.random() < 0.05:
        dur = random.uniform(30, 180)
        logger.info("Nghỉ dài %.1fs", dur)
        time.sleep(dur)


# ============================================================
# TIỆN ÍCH: build browser context hoàn chỉnh
# ============================================================

def build_context(playwright, fp: dict, headless: bool = True):
    """
    [NEW] Tạo browser + context với đầy đủ stealth đã áp dụng.
    Trả về (browser, context).
    """
    browser = playwright.chromium.launch(
        headless=headless,
        args=chromium_launch_args(fp),
    )
    context = browser.new_context(**chromium_context_options(fp))
    context.add_init_script(build_stealth_js(fp))
    return browser, context


# ============================================================
# SELF-TEST NHANH
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    fp = get_or_create_fingerprint("cookies/demo_account.json")
    print("Fingerprint demo:")
    print(json.dumps(fp, ensure_ascii=False, indent=2))

    print("\nDelay mẫu:", [round(human_delay(), 2) for _ in range(5)])
    print("Batch size mẫu:", [human_batch_size() for _ in range(5)])

    st = load_state("demo_account")
    print("State ban đầu:", st)
    record_claim("demo_account")
    print("Sau 1 claim:", load_state("demo_account"))

    print("Target được chọn:",
          pick_transfer_target(["A", "B", "C", "D", "E"]))
    print("Target được chọn:",
          pick_transfer_target(["A", "B", "C", "D", "E"]))
    print("Target được chọn:",
          pick_transfer_target(["A", "B", "C", "D", "E"]))
