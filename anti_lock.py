# anti_lock.py
# -*- coding: utf-8 -*-
"""
Anti-detect / anti-lock module cho jieqi-core.

BẢN ĐẦY ĐỦ — v5 (khớp 100% API mà ck15.py gọi)

API ck15.py yêu cầu:
    # Hằng số
    DEFAULT_MAX_CLAIMS_PER_DAY
    DEFAULT_MAX_TRANSFERS_PER_DAY
    DEFAULT_MAX_TRANSFER_AMOUNT

    # Hàm
    get_or_create_fingerprint(cookie_file) -> dict
    stealth_launch_args() -> list
    build_stealth_js(fp) -> str
    warm_up_account(page, max_s=15)
    check_daily_quota(cookie_file, max_claims=, max_transfers=, max_amount=) -> (bool, str)
    record_session_start(cookie_file)
    random_batch_size(base, lo=, hi=) -> int
    detect_soft_ban(text_or_response) -> bool
    set_soft_ban(cookie_file, duration_s=900) -> float
    record_transfer(cookie_file, amt)
    jitter_sleep(a, b)              # SLEEP
    record_claim(cookie_file, amount)
    maybe_idle_browse(page, claim_count, every=N)
    human_delay(min_s, max_s)       # SLEEP (KHÔNG phải return float)
    random_session_offset(jitter) -> float

  
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
from collections import deque
from datetime import date
from typing import Any, Optional, Iterable, Union, Tuple, Dict

logger = logging.getLogger(__name__)

# ============================================================
# PATHS & HẰNG SỐ TOÀN CỤC
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FINGERPRINT_DIR = os.path.join(BASE_DIR, "fingerprints")
STATE_DIR = os.path.join(BASE_DIR, "state")
os.makedirs(FINGERPRINT_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

PROFILE_VERSION = 2

# Hằng số ck15.py đọc qua getattr(al, ..., default)
DEFAULT_MAX_CLAIMS_PER_DAY = 180
DEFAULT_MAX_TRANSFERS_PER_DAY = 30
DEFAULT_MAX_TRANSFER_AMOUNT = 2_000_000

DAILY_CLAIM_LIMIT = DEFAULT_MAX_CLAIMS_PER_DAY
DAILY_TRANSFER_LIMIT = DEFAULT_MAX_TRANSFERS_PER_DAY
DEFAULT_SOFT_BAN_SECONDS = 900


# ============================================================
# A) FINGERPRINT PER-ACCOUNT
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
        "color_scheme": "light",
        "webgl": {
            "vendor": plat["gl_vendor"],
            "renderer": renderer,
            "unmasked_vendor": plat["gl_vendor"],
            "unmasked_renderer": renderer,
        },
        "languages": rng.choice(LANGUAGES_POOL),
    }


def get_or_create_fingerprint(cookie_path: str) -> dict:
    """Fingerprint ổn định cho 1 account, có version để rebuild."""
    seed = _seed_from_cookie_path(cookie_path)
    fp_path = os.path.join(FINGERPRINT_DIR, f"fp_{seed:016x}.json")

    if os.path.exists(fp_path):
        try:
            with open(fp_path, "r", encoding="utf-8") as f:
                fp = json.load(f)
            if fp.get("profile_version") == PROFILE_VERSION:
                return fp
            logger.info("Fingerprint cũ (v%s) → rebuild cho %s",
                        fp.get("profile_version"), cookie_path)
        except Exception as e:
            logger.warning("Không đọc được fingerprint cũ (%s), tạo mới.", e)

    fp = _build_fingerprint(seed)
    with open(fp_path, "w", encoding="utf-8") as f:
        json.dump(fp, f, ensure_ascii=False, indent=2)
    return fp


def rotate_fingerprint(cookie_path: str) -> dict:
    seed = _seed_from_cookie_path(cookie_path)
    fp_path = os.path.join(FINGERPRINT_DIR, f"fp_{seed:016x}.json")
    if os.path.exists(fp_path):
        os.remove(fp_path)
    return get_or_create_fingerprint(cookie_path)


# ============================================================
# B) STEALTH JS INJECTION
# ============================================================
def build_stealth_js(fp: dict) -> str:
    canvas_jitter = fp["seed"] % 3
    return f"""
(() => {{
  if (window.__stealth_applied__) return;

  try {{
    Object.defineProperty(Navigator.prototype, 'webdriver', {{
      get: () => false, configurable: true,
    }});
  }} catch (e) {{}}

  try {{
    Object.defineProperty(Navigator.prototype, 'languages', {{
      get: () => {json.dumps(fp['languages'])}, configurable: true,
    }});
    Object.defineProperty(Navigator.prototype, 'language', {{
      get: () => {json.dumps(fp['languages'][0])}, configurable: true,
    }});
  }} catch (e) {{}}

  try {{
    Object.defineProperty(Navigator.prototype, 'platform', {{
      get: () => {json.dumps(fp['platform'])}, configurable: true,
    }});
    Object.defineProperty(Navigator.prototype, 'vendor', {{
      get: () => {json.dumps(fp['vendor'])}, configurable: true,
    }});
  }} catch (e) {{}}

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

  try {{
    Object.defineProperty(Notification, 'permission', {{
      get: () => 'default', configurable: true,
    }});
  }} catch (e) {{}}

  window.__stealth_applied__ = true;
}})();
"""


def apply_stealth(page, fp: dict, context=None) -> None:
    try:
        page.add_init_script(build_stealth_js(fp))
    except Exception as e:
        logger.warning("add_init_script lỗi: %s", e)
    if context is not None:
        try:
            context.add_init_script(build_stealth_js(fp))
        except Exception:
            pass


# ============================================================
# C) TIMING — SLEEPING HELPERS
# ------------------------------------------------------------
# ⚠️ QUAN TRỌNG: human_delay và jitter_sleep PHẢI tự sleep.
# ck15.py gọi:
#     al.human_delay(max(2.0, DELAY), max(4.0, DELAY+3.0))   # SLEEP
#     al.human_delay(max(3.0, DELAY), max(8.0, DELAY+5.0))   # SLEEP
#     al.jitter_sleep(1.0, 1.0)                              # SLEEP
#     al.jitter_sleep(1.0, 0.8)                              # SLEEP
# KHÔNG wrap trong time.sleep() → hàm phải sleep bên trong.
# ============================================================

def human_delay(min_s: float = 1.0, max_s: float = 3.0,
                *args, **kwargs) -> float:
    """
    Sleep ngẫu nhiên trong [min_s, max_s] giây.

    ck15.py gọi: al.human_delay(3.0, 6.0)  → sleep uniform(3, 6)
                  al.human_delay(10.0, 13.0) → sleep uniform(10, 13)

    Trả về duration đã sleep (float).
    """
    try:
        lo = float(min_s)
        hi = float(max_s if max_s is not None else min_s)
    except Exception:
        lo, hi = 1.0, 3.0

    if hi < lo:
        lo, hi = hi, lo

    # 20% trường hợp ưu tiên nửa trên (đuôi dài giống người thật)
    if hi > lo:
        if random.random() < 0.2:
            d = random.uniform((lo + hi) / 2.0, hi)
        else:
            d = random.uniform(lo, hi)
    else:
        d = lo

    time.sleep(d)
    return d


def jitter_sleep(a: float = 1.0, b: float = 1.0,
                 *args, **kwargs) -> float:
    """
    Alias của human_delay — sleep ngẫu nhiên.
    ck15.py gọi: al.jitter_sleep(1.0, 1.0) / al.jitter_sleep(1.0, 0.8)
    """
    return human_delay(a, b)


# Alias
sleep_human = human_delay
jitter = jitter_sleep
random_sleep = jitter_sleep
human_sleep = jitter_sleep
random_delay = human_delay
rand_delay = human_delay
humanize_delay = human_delay


def _human_delay_value(min_s: float = 1.0, max_s: float = 3.0) -> float:
    """Chỉ trả về giá trị, KHÔNG sleep (dùng khi cần tính toán)."""
    try:
        lo = float(min_s)
        hi = float(max_s if max_s is not None else min_s)
    except Exception:
        lo, hi = 1.0, 3.0
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


def human_batch_size(lo: int = 3, hi: int = 8) -> int:
    return random.randint(lo, hi)


# ---------- random_batch_size (ck15.py) ----------
def random_batch_size(base: int = 40,
                      lo: Optional[int] = None,
                      hi: Optional[int] = None,
                      *args, **kwargs) -> int:
    """
    ck15.py gọi: al.random_batch_size(CLAIM_BATCH, lo=max(10, CLAIM_BATCH-5), hi=CLAIM_BATCH+5)
    """
    if lo is None:
        lo = max(1, int(base * 0.75))
    if hi is None:
        hi = int(base * 1.25)
    lo = max(1, int(lo))
    hi = max(lo, int(hi))
    return random.randint(lo, hi)


batch_size = random_batch_size
jitter_batch = random_batch_size


# ---------- random_session_offset (HÀM GÂY LỖI CI) ----------
def random_session_offset(jitter: Union[int, float, Tuple, List, None] = None,
                          *args, **kwargs) -> float:
    """
    ck15.py gọi: al.random_session_offset(SESSION_START_JITTER)
    với SESSION_START_JITTER = int(os.environ.get("SESSION_START_JITTER", "180"))

    Trả về offset (giây) ngẫu nhiên:
        random_session_offset(180)          → uniform(0, 180)
        random_session_offset((10, 60))     → uniform(10, 60)
        random_session_offset(min=5, max=90)
    """
    lo, hi = 0.0, 60.0

    if isinstance(jitter, (int, float)) and not isinstance(jitter, bool):
        hi = float(jitter)
    elif isinstance(jitter, (tuple, list)) and len(jitter) >= 2:
        lo, hi = float(jitter[0]), float(jitter[1])
    else:
        lo = float(kwargs.get("min", kwargs.get("lo", lo)))
        hi = float(kwargs.get("max", kwargs.get("hi", hi)))

    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


session_offset = random_session_offset
random_jitter = random_session_offset
random_start_offset = random_session_offset
random_start_delay = random_session_offset
startup_jitter = random_session_offset


# ============================================================
# D) STATE / QUOTA (ATOMIC)
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
        "transfer_amount": 0,
        "sessions": 0,
        "soft_ban_until": 0,
        "consecutive_failures": 0,
        "claim_limit": DEFAULT_MAX_CLAIMS_PER_DAY,
        "transfer_limit": DEFAULT_MAX_TRANSFERS_PER_DAY,
        "amount_limit": DEFAULT_MAX_TRANSFER_AMOUNT,
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

    if st.get("date") != date.today().isoformat():
        st = _default_state()
    for k, v in _default_state().items():
        st.setdefault(k, v)
    return st


def save_state(account_id: str, st: dict) -> None:
    st["last_update"] = time.time()
    _atomic_write_json(_state_path(account_id), st)


# ============================================================
# E) QUOTA — API GỐC
# ============================================================
def check_daily_quota(account_id: Any = None,
                      action: Optional[str] = None,
                      *args, **kwargs) -> Tuple[bool, str]:
    """
    Kiểm tra quota hàng ngày.

    ck15.py gọi:
        al.check_daily_quota(
            SINGLE_COOKIE_FILE,
            max_claims=MAX_CLAIMS_PER_DAY,
            max_transfers=MAX_TRANSFERS_PER_DAY,
            max_amount=MAX_TRANSFER_AMOUNT_PER_DAY,
        )
    → action mặc định None → kiểm tra TẤT CẢ (claim + transfer + amount),
      trả về False nếu BẤT KỲ hạn mức nào đã đạt.

    Cũng hỗ trợ:
        check_daily_quota(acc, "claim")
        check_daily_quota(acc, action="transfer")
        check_daily_quota(state_dict)

    Trả về: (ok: bool, reason: str)
    """
    if isinstance(account_id, dict):
        st = account_id
    else:
        if account_id is None:
            account_id = (kwargs.get("account") or kwargs.get("account_id")
                          or kwargs.get("aid") or "default")
        st = load_state(str(account_id))

    # Cập nhật giới hạn từ kwargs
    if "max_claims" in kwargs:
        st["claim_limit"] = int(kwargs["max_claims"])
    if "max_transfers" in kwargs:
        st["transfer_limit"] = int(kwargs["max_transfers"])
    if "max_amount" in kwargs:
        st["amount_limit"] = int(kwargs["max_amount"])

    # 1) Soft-ban?
    now = time.time()
    until = st.get("soft_ban_until", 0) or 0
    if until and now < until:
        return False, f"soft_ban còn {int(until - now)}s"

    # 2) Chọn action
    act = (action or kwargs.get("kind") or kwargs.get("type")
           or kwargs.get("category"))

    # 3) Nếu action=None → kiểm tra cả 3
    checks = []
    if act is None:
        checks = ["claim", "transfer", "amount"]
    else:
        checks = [act.lower()]

    for c in checks:
        if c in ("claim", "claims", "reward", "rewards", "collect"):
            used = st.get("claims", 0)
            limit = st.get("claim_limit", DEFAULT_MAX_CLAIMS_PER_DAY)
            label = "claim"
        elif c in ("transfer", "transfers", "send", "gift", "give"):
            used = st.get("transfers", 0)
            limit = st.get("transfer_limit", DEFAULT_MAX_TRANSFERS_PER_DAY)
            label = "transfer"
        elif c in ("amount", "transfer_amount"):
            used = st.get("transfer_amount", 0)
            limit = st.get("amount_limit", DEFAULT_MAX_TRANSFER_AMOUNT)
            label = "amount"
        else:
            continue
        if limit and used >= limit:
            return False, f"{label} limit reached ({used}/{limit})"

    return True, "ok"


check_quota = check_daily_quota
check_daily_limit = check_daily_quota
check_limit = check_daily_quota


def record_claim(account_id: str, amount: int = 1) -> None:
    """ck15.py: al.record_claim(SINGLE_COOKIE_FILE, amount)"""
    st = load_state(account_id)
    st["claims"] = st.get("claims", 0) + 1
    save_state(account_id, st)


def record_transfer(account_id: str, amount: int = 0) -> None:
    """ck15.py: al.record_transfer(SINGLE_COOKIE_FILE, amt)"""
    st = load_state(account_id)
    st["transfers"] = st.get("transfers", 0) + 1
    st["transfer_amount"] = st.get("transfer_amount", 0) + int(amount or 0)
    save_state(account_id, st)


def record_session_start(account_id: str) -> None:
    """ck15.py: al.record_session_start(SINGLE_COOKIE_FILE)"""
    st = load_state(account_id)
    st["sessions"] = st.get("sessions", 0) + 1
    save_state(account_id, st)


increment_claim = record_claim
increment_transfer = record_transfer
mark_claim = record_claim
mark_transfer = record_transfer
record_session = record_session_start


def reset_daily(account_id: str) -> None:
    save_state(account_id, _default_state())


def reset_failures(account_id: str) -> None:
    st = load_state(account_id)
    if st.get("consecutive_failures", 0) != 0:
        st["consecutive_failures"] = 0
        save_state(account_id, st)


def backoff(account_id: str, base_seconds: int = 300) -> int:
    st = load_state(account_id)
    st["consecutive_failures"] = st.get("consecutive_failures", 0) + 1
    n = st["consecutive_failures"]
    cool = min(base_seconds * (2 ** (n - 1)), 6 * 3600)
    cool = int(cool * random.uniform(0.85, 1.15))
    st["soft_ban_until"] = time.time() + cool
    save_state(account_id, st)
    logger.warning("Backoff account=%s trong %ss (fail lần %d)",
                   account_id, cool, n)
    return cool


# ============================================================
# F) SOFT-BAN
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
    r"blocked",
    r"bị khóa",
    r"bị chặn",
]
_SOFT_BAN_RE = re.compile("|".join(SOFT_BAN_KEYWORDS), re.IGNORECASE)


def detect_soft_ban(response: Any = None,
                    status: Optional[int] = None,
                    body_text: Optional[str] = None,
                    *args, **kwargs) -> bool:
    """
    ck15.py gọi:
        al.detect_soft_ban(msg)
        al.detect_soft_ban(r.get("error", ""))
        al.detect_soft_ban(result.get("error", ""))
        al.detect_soft_ban(result.get("message", ""))
    → arg đầu có thể là str (message) hoặc response object.
    """
    if isinstance(response, str) and body_text is None:
        body_text = response
        response = None

    if status in (429, 403, 503):
        return True

    if response is not None:
        s = getattr(response, "status", None) or getattr(response, "status_code", None)
        if s in (429, 403, 503):
            return True

    text = body_text
    if text is None and response is not None:
        t = getattr(response, "text", None)
        if callable(t):
            try:
                text = t()
            except Exception:
                text = None
        else:
            text = t

    if not text:
        return False
    return bool(_SOFT_BAN_RE.search(str(text)))


is_soft_banned = detect_soft_ban
is_rate_limited = detect_soft_ban
is_blocked = detect_soft_ban


def set_soft_ban(account_id: str, duration_s: int = DEFAULT_SOFT_BAN_SECONDS,
                 reason: str = "") -> float:
    """ck15.py: al.set_soft_ban(SINGLE_COOKIE_FILE, duration_s=900)"""
    st = load_state(account_id)
    until = time.time() + int(duration_s)
    st["soft_ban_until"] = until
    save_state(account_id, st)
    logger.warning("Soft-ban %s trong %ss (lý do: %s)",
                   account_id, duration_s, reason or "không rõ")
    return until


apply_soft_ban = set_soft_ban
mark_soft_ban = set_soft_ban


# ============================================================
# G) LUÂN CHUYỂN ĐÍCH TRANSFER
# ============================================================
_target_history: deque = deque(maxlen=3)


def pick_transfer_target(targets: Iterable[str]) -> Optional[str]:
    pool = [t for t in targets if t]
    if not pool:
        return None
    recent = set(_target_history)
    candidates = [t for t in pool if t not in recent]
    if not candidates:
        last = _target_history[-1] if _target_history else None
        candidates = [t for t in pool if t != last] or pool
    chosen = random.choice(candidates)
    _target_history.append(chosen)
    return chosen


choose_target = pick_transfer_target
select_transfer_target = pick_transfer_target


# ============================================================
# H) CHROMIUM LAUNCH ARGS & CONTEXT OPTIONS
# ============================================================
def stealth_launch_args(fp: Optional[dict] = None) -> list:
    """ck15.py gọi: al.stealth_launch_args()  — KHÔNG truyền tham số."""
    args = [
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
    ]
    if fp:
        try:
            w = fp["viewport"]["width"]
            h = fp["viewport"]["height"]
            args += [f"--window-size={w},{h}", f"--lang={fp['locale']}"]
        except Exception:
            pass
    return args


chromium_launch_args = stealth_launch_args


def chromium_context_options(fp: dict) -> dict:
    chrome_major = fp["chrome_version"].split(".")[0]
    return {
        "user_agent": fp["user_agent"],
        "viewport": fp["viewport"],
        "screen": fp["screen"],
        "locale": fp["locale"],
        "timezone_id": fp["timezone"],
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": fp.get("color_scheme", "light"),
        "extra_http_headers": {
            "Accept-Language": f"{fp['locale']},"
                              f"{fp['locale'].split('-')[0]};q=0.9,en;q=0.8",
            "sec-ch-ua": (
                f'"Chromium";v="{chrome_major}", '
                f'"Google Chrome";v="{chrome_major}", '
                f'"Not?A_Brand";v="24"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{fp["platform"]}"',
        },
    }


def build_context(playwright, fp: dict, headless: bool = True):
    browser = playwright.chromium.launch(
        headless=headless,
        args=stealth_launch_args(fp),
    )
    context = browser.new_context(**chromium_context_options(fp))
    context.add_init_script(build_stealth_js(fp))
    return browser, context


# ============================================================
# I) HÀNH VI NGƯỜI
# ============================================================
def warm_up_account(page, max_s: float = 15.0) -> None:
    """ck15.py: al.warm_up_account(page, max_s=15)"""
    try:
        end = time.time() + float(max_s)
        while time.time() < end:
            delta = random.randint(100, 500)
            steps = random.randint(3, 6)
            for _ in range(steps):
                try:
                    page.mouse.wheel(0, delta // steps)
                except Exception:
                    break
                time.sleep(random.uniform(0.05, 0.15))
            if random.random() < 0.4:
                time.sleep(random.uniform(0.5, 2.0))
    except Exception as e:
        logger.debug("warm_up_account lỗi: %s", e)


warmup = warm_up_account


def human_scroll(page, max_scrolls: int = 5) -> None:
    try:
        for _ in range(random.randint(1, max_scrolls)):
            delta = random.randint(200, 900)
            steps = random.randint(3, 8)
            for _ in range(steps):
                page.mouse.wheel(0, delta // steps)
                time.sleep(random.uniform(0.03, 0.12))
            if random.random() < 0.35:
                time.sleep(random.uniform(1.0, 4.0))
    except Exception as e:
        logger.debug("human_scroll lỗi: %s", e)


def human_idle(page, min_s: float = 5.0, max_s: float = 20.0) -> None:
    dur = random.uniform(min_s, max_s)
    logger.info("Idle %.1fs", dur)
    t_end = time.time() + dur
    while time.time() < t_end:
        try:
            human_scroll(page, max_scrolls=2)
        except Exception:
            pass
        time.sleep(random.uniform(0.5, 2.0))


# ---------- maybe_idle_browse (ck15.py MỚI cần) ----------
def maybe_idle_browse(page, claim_count: int, every: int = 60,
                      min_s: float = 10.0, max_s: float = 30.0,
                      *args, **kwargs) -> bool:
    """
    Cứ mỗi `every` claim thì nghỉ 1 nhịp scroll feed.

    ck15.py gọi:
        al.maybe_idle_browse(page, claim_count, every=IDLE_EVERY_CLAIMS)

    Trả về True nếu CÓ nghỉ, False nếu bỏ qua.
    """
    try:
        every = int(every or 0)
        claim_count = int(claim_count or 0)
    except Exception:
        return False

    if every <= 0 or claim_count <= 0 or claim_count % every != 0:
        return False

    dur = random.uniform(min_s, max_s)
    logger.info("[ANTI-BAN] Idle browse %.1fs sau %d claims", dur, claim_count)
    t_end = time.time() + dur

    while time.time() < t_end:
        try:
            delta = random.randint(200, 800)
            steps = random.randint(3, 6)
            for _ in range(steps):
                page.mouse.wheel(0, delta // steps)
                time.sleep(random.uniform(0.05, 0.15))
            time.sleep(random.uniform(0.5, 2.0))
        except Exception:
            time.sleep(1.0)
    return True


idle_browse = maybe_idle_browse


def maybe_take_break() -> None:
    if random.random() < 0.05:
        dur = random.uniform(30, 180)
        logger.info("Nghỉ dài %.1fs", dur)
        time.sleep(dur)


scroll_like_human = human_scroll
idle_like_human = human_idle
take_break = maybe_take_break


# ============================================================
# SELF-TEST
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    print("=== Test API ck15.py cần ===")
    print("Hằng số:",
          DEFAULT_MAX_CLAIMS_PER_DAY,
          DEFAULT_MAX_TRANSFERS_PER_DAY,
          DEFAULT_MAX_TRANSFER_AMOUNT)

    print("\nrandom_session_offset(180):", random_session_offset(180))
    print("random_session_offset((10, 60)):", random_session_offset((10, 60)))
    print("random_batch_size(40, lo=35, hi=45):",
          random_batch_size(40, lo=35, hi=45))

    print("\ncheck_daily_quota('acc1', max_claims=180, max_transfers=30, max_amount=2e6):",
          check_daily_quota("acc1", max_claims=180,
                            max_transfers=30, max_amount=2_000_000))

    print("\ndetect_soft_ban('bị khóa'):", detect_soft_ban("bị khóa"))
    print("detect_soft_ban(status=429):", detect_soft_ban(status=429))

    print("\nstealth_launch_args()[:3]:", stealth_launch_args()[:3])

    print("\n--- human_delay SLEEP test ---")
    t0 = time.time()
    dur = human_delay(1.0, 2.0)
    print(f"human_delay(1.0, 2.0) slept {time.time()-t0:.2f}s (returned {dur:.2f})")

    t0 = time.time()
    jitter_sleep(1.0, 1.0)
    print(f"jitter_sleep(1.0, 1.0) slept {time.time()-t0:.2f}s")

    print("\n--- maybe_idle_browse test ---")
    class FakePage:
        class mouse:
            @staticmethod
            def wheel(a, b): pass
    print("claim=59, every=60 ->",
          maybe_idle_browse(FakePage(), 59, every=60))
    t0 = time.time()
    print("claim=60, every=60 ->",
          maybe_idle_browse(FakePage(), 60, every=60, min_s=0.5, max_s=1.0))
    print(f"  (mất {time.time()-t0:.2f}s)")

    # Test record
    record_claim("demo", 100)
    record_transfer("demo", 5000)
    record_session_start("demo")
    set_soft_ban("demo", duration_s=60)
    print("\nState demo:", load_state("demo"))
