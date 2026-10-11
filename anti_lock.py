# anti_lock.py
# -*- coding: utf-8 -*-
"""
Anti-detect / anti-lock module cho jieqi-core.

BẢN ĐẦY ĐỦ — v3
- Giữ API gốc (backward-compat): check_daily_quota(), record_claim(),
  record_transfer(), detect_soft_ban(), human_delay(), v.v.
- Kèm cải tiến: fingerprint có version, stealth JS fix, TLS header,
  quota atomic, back-off động, log-normal timing.


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
# PATHS & HẰNG SỐ
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FINGERPRINT_DIR = os.path.join(BASE_DIR, "fingerprints")
STATE_DIR = os.path.join(BASE_DIR, "state")
os.makedirs(FINGERPRINT_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

PROFILE_VERSION = 2

DAILY_CLAIM_LIMIT = 25
DAILY_TRANSFER_LIMIT = 15

# Ngưỡng soft-ban mặc định (giây) — có thể override qua state
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
    """Xoá fingerprint cũ để buộc tạo mới (dùng sau khi bị flag nặng)."""
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
    """Gọi TRƯỚC page.goto()."""
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
# C) TIMING & BATCH SIZE
# ============================================================
def human_delay(base: float = 2.0, spread: float = 0.6,
                min_s: float = 0.4, max_s: float = 20.0) -> float:
    if random.random() < 0.05:
        return random.uniform(max_s, max_s * 2)
    mu = math.log(max(base, 1e-3))
    d = random.lognormvariate(mu, spread)
    return max(min_s, min(max_s, d))


def sleep_human(base: float = 2.0, spread: float = 0.6) -> None:
    time.sleep(human_delay(base, spread))


def human_batch_size(lo: int = 3, hi: int = 8) -> int:
    return random.randint(lo, hi)


# Alias cũ — nếu ck20.py gọi tên khác
random_delay = human_delay
rand_delay = human_delay
humanize_delay = human_delay
get_human_delay = human_delay


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
        "soft_ban_until": 0,
        "consecutive_failures": 0,
        "claim_limit": DAILY_CLAIM_LIMIT,
        "transfer_limit": DAILY_TRANSFER_LIMIT,
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
# E) QUOTA — API GỐC (BACKWARD-COMPAT)
# ------------------------------------------------------------
# Đây là hàm ck20.py gọi ở dòng 932.
# Signature linh hoạt để chấp nhận mọi cách gọi cũ.
# ============================================================
def check_daily_quota(account_id: Any = None,
                      action: str = "claim",
                      *args, **kwargs) -> Tuple[bool, str]:
    """
    Kiểm tra quota hàng ngày.

    Trả về: (ok: bool, reason: str)

    Các kiểu gọi được hỗ trợ:
        check_daily_quota("acc1")
        check_daily_quota("acc1", "claim")
        check_daily_quota("acc1", action="transfer")
        check_daily_quota(account_id="acc1", kind="claim")
        check_daily_quota(state_dict)          # truyền thẳng state
    """
    # Cho phép truyền state dict trực tiếp
    if isinstance(account_id, dict):
        st = account_id
        acc_label = st.get("account_id", "inline")
    else:
        # Hỗ trợ kwargs thay thế
        if account_id is None:
            account_id = (kwargs.get("account") or kwargs.get("account_id")
                          or kwargs.get("aid") or "default")
        acc_label = str(account_id)
        st = load_state(acc_label)

    # Alias action từ kwargs
    act = (action or kwargs.get("kind") or kwargs.get("type")
           or kwargs.get("category") or "claim").lower()

    # 1) Đang soft-ban?
    now = time.time()
    until = st.get("soft_ban_until", 0) or 0
    if until and now < until:
        remain = int(until - now)
        return False, f"soft_ban còn {remain}s"

    # 2) Phân loại action
    if act in ("claim", "claims", "reward", "rewards", "collect"):
        used = st.get("claims", 0)
        limit = st.get("claim_limit", DAILY_CLAIM_LIMIT)
        label = "claim"
    elif act in ("transfer", "transfers", "send", "gift", "give"):
        used = st.get("transfers", 0)
        limit = st.get("transfer_limit", DAILY_TRANSFER_LIMIT)
        label = "transfer"
    else:
        # Không rõ → cho qua nhưng ghi rõ để dễ debug
        return True, f"unknown_action:{act}"

    if used >= limit:
        return False, f"{label} limit reached ({used}/{limit})"

    return True, f"ok ({used}/{limit})"


# Aliases — phòng khi ck20.py gọi tên khác
check_quota = check_daily_quota
check_daily_limit = check_daily_quota
check_limit = check_daily_quota
can_claim = lambda aid=None, **kw: check_daily_quota(aid, "claim", **kw)[0]
can_transfer = lambda aid=None, **kw: check_daily_quota(aid, "transfer", **kw)[0]


def record_claim(account_id: str, n: int = 1) -> None:
    """Ghi nhận đã claim (mặc định +1). Chấp nhận n để tăng nhiều lần."""
    st = load_state(account_id)
    st["claims"] = st.get("claims", 0) + max(1, int(n))
    save_state(account_id, st)


def record_transfer(account_id: str, n: int = 1) -> None:
    st = load_state(account_id)
    st["transfers"] = st.get("transfers", 0) + max(1, int(n))
    save_state(account_id, st)


# Alias cũ
increment_claim = record_claim
increment_transfer = record_transfer
mark_claim = record_claim
mark_transfer = record_transfer


def reset_daily(account_id: str) -> None:
    """Reset quota cho hôm nay (thường không cần gọi)."""
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


# Alias cũ
soft_ban = backoff
apply_backoff = backoff


# ============================================================
# F) PHÁT HIỆN SOFT-BAN
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


def detect_soft_ban(response: Any = None,
                    status: Optional[int] = None,
                    body_text: Optional[str] = None,
                    *args, **kwargs) -> bool:
    """
    Trả về True nếu có dấu hiệu soft-ban.
    Chấp nhận: (response), (status), (body_text), hoặc kết hợp.
    """
    # status trực tiếp
    if status in (429, 403, 503):
        return True

    # response object
    if response is not None:
        s = getattr(response, "status", None) or getattr(response, "status_code", None)
        if s in (429, 403, 503):
            return True

    # text
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

    if _SOFT_BAN_RE.search(str(text)):
        return True
    return False


# Alias cũ
is_soft_banned = detect_soft_ban
is_rate_limited = detect_soft_ban
is_blocked = detect_soft_ban


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


# Alias cũ
choose_target = pick_transfer_target
select_transfer_target = pick_transfer_target


# ============================================================
# H) CHROMIUM LAUNCH ARGS & CONTEXT OPTIONS
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
    ]


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
        "color_scheme": "light",
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
    """Tạo browser + context với stealth đã áp dụng."""
    browser = playwright.chromium.launch(
        headless=headless,
        args=chromium_launch_args(fp),
    )
    context = browser.new_context(**chromium_context_options(fp))
    context.add_init_script(build_stealth_js(fp))
    return browser, context


# ============================================================
# I) HÀNH VI NGƯỜI
# ============================================================
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
    logger.info("Idle %.1fs (giả lập người dùng nghỉ)", dur)
    t_end = time.time() + dur
    while time.time() < t_end:
        try:
            human_scroll(page, max_scrolls=2)
        except Exception:
            pass
        time.sleep(random.uniform(0.5, 2.0))


def maybe_take_break() -> None:
    if random.random() < 0.05:
        dur = random.uniform(30, 180)
        logger.info("Nghỉ dài %.1fs", dur)
        time.sleep(dur)


# Alias cũ
scroll_like_human = human_scroll
idle_like_human = human_idle
take_break = maybe_take_break


# ============================================================
# SELF-TEST
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    fp = get_or_create_fingerprint("cookies/demo_account.json")
    print("Fingerprint demo:")
    print(json.dumps(fp, ensure_ascii=False, indent=2))

    print("\nDelay mẫu:", [round(human_delay(), 2) for _ in range(5)])
    print("Batch size mẫu:", [human_batch_size() for _ in range(5)])

    # Test API gốc (cái đang gây lỗi)
    print("\n--- check_daily_quota ---")
    print("claim:", check_daily_quota("demo_account"))
    print("transfer:", check_daily_quota("demo_account", "transfer"))
    print("alias check_quota:", check_quota("demo_account", action="claim"))

    record_claim("demo_account")
    print("Sau 1 claim:", load_state("demo_account"))

    print("\nTarget:", pick_transfer_target(["A", "B", "C", "D", "E"]))
    print("Target:", pick_transfer_target(["A", "B", "C", "D", "E"]))
    print("Target:", pick_transfer_target(["A", "B", "C", "D", "E"]))

    print("\nsoft_ban test:",
          detect_soft_ban(body_text="Bạn đã bị tạm thời khóa, vui lòng thử lại sau"))
    print("soft_ban HTTP 429:", detect_soft_ban(status=429))
    print("soft_ban bình thường:",
          detect_soft_ban(body_text="Chúc mừng, bạn nhận được 100 điểm"))
