#!/usr/bin/env python3
"""
anti_lock_stealth.py — bản stealth MẠNH hơn anti_lock.py

Giữ nguyên toàn bộ API của anti_lock (import anti_lock as base),
chỉ override / thêm:
  - build_stealth_js()  → patch sâu hơn (chrome, connection, screen,
                          mediaDevices, battery, speech, canvas noise,
                          iframe contentWindow, permissions, WebGL2, etc.)
  - stealth_launch_args() → thêm cờ Chromium chống detect
  - apply_stealth(page, fp) → 1 hàm duy nhất: init_script + CDP + UA/viewport

Dùng khi site check fingerprint sâu (FB, game portal, anti-bot).
Còn site nhẹ → anti_lock.py là đủ.
"""
from __future__ import annotations

import os
import random
import time

try:
    import anti_lock as base
except ImportError:  # chạy standalone trong cùng thư mục
    import importlib.util as _ilu
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anti_lock.py")
    _spec = _ilu.spec_from_file_location("anti_lock", _p)
    base = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(base)

# Re-export API cũ — caller anti_lock.py không cần đổi
FP_DIR = base.FP_DIR
STATE_DIR = base.STATE_DIR
DEST_FILE = base.DEST_FILE
USER_AGENTS = base.USER_AGENTS
VIEWPORTS = base.VIEWPORTS
LOCALES = base.LOCALES
TIMEZONES = base.TIMEZONES
SOFT_BAN_HINTS = base.SOFT_BAN_HINTS
DEFAULT_MAX_CLAIMS_PER_DAY = base.DEFAULT_MAX_CLAIMS_PER_DAY
DEFAULT_MAX_TRANSFERS_PER_DAY = base.DEFAULT_MAX_TRANSFERS_PER_DAY
DEFAULT_MAX_TRANSFER_AMOUNT = base.DEFAULT_MAX_TRANSFER_AMOUNT

get_or_create_fingerprint = base.get_or_create_fingerprint
human_delay = base.human_delay
jitter_sleep = base.jitter_sleep
random_batch_size = base.random_batch_size
random_session_offset = base.random_session_offset
load_daily_state = base.load_daily_state
save_daily_state = base.save_daily_state
check_daily_quota = base.check_daily_quota
record_claim = base.record_claim
record_transfer = base.record_transfer
record_session_start = base.record_session_start
set_soft_ban = base.set_soft_ban
detect_soft_ban = base.detect_soft_ban
load_transfer_dests = base.load_transfer_dests
pick_next_dest = base.pick_next_dest
maybe_idle_browse = base.maybe_idle_browse
warm_up_account = base.warm_up_account


# =====================================================================
# STEALTH JS — mạnh hơn anti_lock.build_stealth_js
# =====================================================================
def build_stealth_js(fp: dict) -> str:
    """JS inject trước page load. Bao phủ thêm vs anti_lock:
    - chrome fully populated (runtime, csi, loadTimes, app, webstore)
    - navigator.connection / getBattery
    - screen / outerWidth-Height khớp viewport
    - mediaDevices.enumerateDevices fake
    - speechSynthesis.getVoices
    - canvas noise nhẹ (fp unique, không break game)
    - iframe.contentWindow.webdriver
    - Notification.permission
    - permissions.query + toString patch
    - WebGL + WebGL2 unmasked vendor/renderer
    - Function.prototype.toString hide for patched fns
    """
    vw = fp.get("viewport") or {"width": 1920, "height": 1080}
    w, h = int(vw.get("width", 1920)), int(vw.get("height", 1080))
    # browser UI roughly 85px chrome on desktop
    inner_w, inner_h = w, max(200, h - 85)
    locale = fp.get("locale", "en-US")
    platform = fp.get("platform", "Win32")
    vendor = fp.get("webgl_vendor", "Google Inc. (Intel)")
    renderer = fp.get("webgl_renderer", "ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.1)")
    hc = int(fp.get("hardware_concurrency", 8))
    dm = int(fp.get("device_memory", 8))
    # canvas seed from fingerprint key so same nick → same noise
    seed = abs(hash(fp.get("user_agent", "x") + str(w))) % 100000

    return f"""() => {{
  const _nt = Function.prototype.toString;
  const _hide = (obj, name, value) => {{
    try {{
      Object.defineProperty(obj, name, {{
        get: () => value,
        configurable: true,
      }});
      if (typeof value === 'function') {{
        value.toString = () => 'function ' + name + '() {{ [native code] }}';
      }}
    }} catch (e) {{}}
  }};

  try {{
    // --- webdriver ---
    _hide(navigator, 'webdriver', undefined);
    try {{ delete Navigator.prototype.webdriver; }} catch (e) {{}}

    // --- platform / languages / uaData ---
    _hide(navigator, 'platform', '{platform}');
    _hide(navigator, 'languages', ['{locale}', 'en-US', 'en']);
    _hide(navigator, 'language', '{locale}');
    if (navigator.userAgentData && navigator.userAgentData.getHighEntropyValues) {{
      try {{
        navigator.userAgentData.getHighEntropyValues = () => Promise.resolve({{
          architecture: 'x86',
          bitness: '64',
          model: '',
          platform: '{platform}'.startsWith('Win') ? 'Windows' :
                    '{platform}'.startsWith('Mac') ? 'macOS' : 'Linux',
          platformVersion: '15.0.0',
          uaFullVersion: '138.0.0.0',
          fullVersionList: [],
          wow64: false,
        }});
      }} catch (e) {{}}
    }}

    // --- hardware ---
    _hide(navigator, 'hardwareConcurrency', {hc});
    _hide(navigator, 'deviceMemory', {dm});
    _hide(navigator, 'maxTouchPoints', 0);
    _hide(navigator, 'pdfViewerEnabled', true);

    // --- plugins (Chrome 3 defaults) ---
    const mkPlugin = (name, filename, desc, mimes) => {{
      const p = {{ name, filename, description: desc, length: mimes.length }};
      mimes.forEach((m, i) => {{ p[i] = m; }});
      p.item = (i) => p[i] || null;
      p.namedItem = (n) => mimes.find(m => m.type === n) || null;
      return Object.freeze(p);
    }};
    const mimePdf = {{ type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }};
    const mimeNative = {{ type: 'application/x-nacl', suffixes: '', description: 'Native Client Executable' }};
    const mimePnacl = {{ type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable' }};
    const plugins = [
      mkPlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format', [mimePdf]),
      mkPlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', [mimePdf]),
      mkPlugin('Native Client', 'internal-nacl-plugin', '', [mimeNative, mimePnacl]),
    ];
    plugins.item = (i) => plugins[i] || null;
    plugins.namedItem = (n) => plugins.find(p => p.name === n) || null;
    plugins.refresh = () => {{}};
    _hide(navigator, 'plugins', plugins);
    const mimeList = [mimePdf, mimeNative, mimePnacl];
    mimeList.item = (i) => mimeList[i] || null;
    mimeList.namedItem = (n) => mimeList.find(m => m.type === n) || null;
    _hide(navigator, 'mimeTypes', mimeList);

    // --- connection (NetworkInformation) ---
    if (!navigator.connection) {{
      try {{
        Object.defineProperty(navigator, 'connection', {{
          get: () => Object.freeze({{
            downlink: 10,
            effectiveType: '4g',
            rtt: 50,
            saveData: false,
            type: 'wifi',
            addEventListener: () => {{}},
            removeEventListener: () => {{}},
            dispatchEvent: () => true,
          }}),
          configurable: true,
        }});
      }} catch (e) {{}}
    }}

    // --- battery ---
    if (navigator.getBattery) {{
      try {{
        navigator.getBattery = () => Promise.resolve(Object.freeze({{
          charging: true,
          chargingTime: 0,
          dischargingTime: Infinity,
          level: 1,
          addEventListener: () => {{}},
          removeEventListener: () => {{}},
        }}));
      }} catch (e) {{}}
    }}

    // --- mediaDevices fake (1 audio + 1 video) ---
    if (navigator.mediaDevices) {{
      try {{
        navigator.mediaDevices.enumerateDevices = () => Promise.resolve([
          {{ deviceId: 'default', kind: 'audioinput', label: '', groupId: 'g1' }},
          {{ deviceId: 'default', kind: 'videoinput', label: '', groupId: 'g2' }},
          {{ deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'g1' }},
        ]);
        navigator.mediaDevices.getUserMedia = () => Promise.reject(
          Object.assign(new DOMException('Permission denied', 'NotAllowedError'), {{ name: 'NotAllowedError' }})
        );
      }} catch (e) {{}}
    }}

    // --- speechSynthesis voices ---
    if (window.speechSynthesis) {{
      try {{
        const voices = [
          {{ name: 'Google US English', lang: 'en-US', localService: false, default: true, voiceURI: 'Google US English' }},
          {{ name: 'Google Tiếng Việt', lang: 'vi-VN', localService: false, default: false, voiceURI: 'Google Tiếng Việt' }},
        ];
        speechSynthesis.getVoices = () => voices;
        speechSynthesis.speak = () => {{}};
        speechSynthesis.pause = () => {{}};
        speechSynthesis.resume = () => {{}};
        speechSynthesis.cancel = () => {{}};
      }} catch (e) {{}}
    }}

    // --- chrome object ---
    const chromeApp = {{
      InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
      RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }},
      getDetails: () => null,
      getIsInstalled: () => false,
      installState: (id, cb) => cb && cb('not_installed'),
      runningState: () => 'cannot_run',
      isInstalled: false,
      InstallState: undefined,
    }};
    window.chrome = window.chrome || {{}};
    window.chrome.runtime = window.chrome.runtime || {{
      PlatformOs: {{ MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' }},
      PlatformArch: {{ ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' }},
      PlatformNaclArch: {{ ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' }},
      RequestUpdateCheckStatus: {{ NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }},
      OnInstalledReason: {{ INSTALL: 'install', UPDATE: 'update', CHROME_UPDATE: 'chrome_update', SHARED_MODULE_UPDATE: 'shared_module_update' }},
      OnRestartRequiredReason: {{ APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }},
      id: undefined,
      connect: () => ({{ onMessage: {{ addListener: () => {{}}, removeListener: () => {{}} }}, postMessage: () => {{}}, disconnect: () => {{}} }}),
      sendMessage: () => {{}},
      PlatformOs: undefined,
    }};
    window.chrome.app = window.chrome.app || chromeApp;
    window.chrome.csi = window.chrome.csi || (() => ({{ startE: Date.now(), onloadT: Date.now(), pageT: Math.round(performance.now()), start: Date.now(), onload: Date.now(), ol: 0, oe: 0, tm: 0 }}));
    window.chrome.loadTimes = window.chrome.loadTimes || (() => ({{ requestTime: Date.now() / 1000 - 0.1, startLoadTime: Date.now() / 1000 - 0.05, commitLoadTime: Date.now() / 1000 - 0.02, finishDocumentLoadTime: Date.now() / 1000, finishLoadTime: Date.now() / 1000 + 0.01, firstPaintTime: Date.now() / 1000 + 0.02, firstPaintAfterLoadTime: 0, navigationType: 'Other', wasFetchedViaSpdy: true, wasNpnNegotiated: true, npnNegotiatedProtocol: 'h2', wasAlternateProtocolAvailable: false, connectionInfo: 'h2' }}));
    window.chrome.webstore = window.chrome.webstore || {{ onDownloadProgress: () => {{}}, onDownloadStarted: () => {{}}, onDownloadFailed: () => {{}}, onDownloadSuccess: () => {{}} }};
    window.chrome.runtime = window.chrome.runtime || {{}};

    // --- screen / window size khớp viewport ---
    try {{
      _hide(screen, 'width', {w});
      _hide(screen, 'height', {h});
      _hide(screen, 'availWidth', {w});
      _hide(screen, 'availHeight', {h - 40});
      _hide(screen, 'colorDepth', 24);
      _hide(screen, 'pixelDepth', 24);
      _hide(screen, 'orientation', {{ type: 'landscape-primary', angle: 0, onchange: null }});
    }} catch (e) {{}}
    try {{
      _hide(window, 'outerWidth', {w});
      _hide(window, 'outerHeight', {h});
      _hide(window, 'innerWidth', {inner_w});
      _hide(window, 'innerHeight', {inner_h});
    }} catch (e) {{}}

    // --- Notification ---
    try {{
      if (window.Notification) {{
        _hide(Notification, 'permission', 'default');
        Notification.requestPermission = () => Promise.resolve('default');
      }}
    }} catch (e) {{}}

    // --- permissions.query ---
    try {{
      const origQuery = navigator.permissions && navigator.permissions.query;
      if (origQuery) {{
        navigator.permissions.query = (params) => {{
          const name = params && params.name;
          const stateMap = {{
            notifications: (typeof Notification !== 'undefined' ? Notification.permission : 'default'),
            geolocation: 'prompt',
            camera: 'prompt',
            microphone: 'prompt',
            'persistent-storage': 'prompt',
            'push-manager': 'prompt',
            'background-sync': 'prompt',
            midi: 'prompt',
            accessibility: 'prompt',
          }};
          if (name && name in stateMap) {{
            return Promise.resolve({{ state: stateMap[name], status: stateMap[name], name, onchange: null }});
          }}
          return origQuery.call(navigator.permissions, params);
        }};
        navigator.permissions.query.toString = () => 'function query() {{ [native code] }}';
      }}
    }} catch (e) {{}}

    // --- WebGL / WebGL2 ---
    const spoofGL = (proto) => {{
      if (!proto || !proto.getParameter) return;
      const orig = proto.getParameter;
      proto.getParameter = function(p) {{
        if (p === 37445) return '{vendor}';
        if (p === 37446) return '{renderer}';
        // MAX_TEXTURE_SIZE / MAX_RENDERBUFFER_SIZE giữ default
        return orig.call(this, p);
      }};
      proto.getParameter.toString = () => 'function getParameter() {{ [native code] }}';
      if (proto.getExtension) {{
        const origExt = proto.getExtension;
        proto.getExtension = function(name) {{
          const r = origExt.call(this, name);
          if (r && (name === 'WEBGL_debug_renderer_info' || name === 'WEBGL_debug_renderer_info')) {{
            return r;
          }}
          return r;
        }};
      }}
    }};
    try {{ spoofGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype); }} catch (e) {{}}
    try {{ spoofGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype); }} catch (e) {{}}

    // --- canvas fingerprint noise (nhẹ, deterministic per seed) ---
    try {{
      const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
      const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
      let n = {seed};
      const rnd = () => {{
        n = (n * 1664525 + 1013904223) >>> 0;
        return (n >>> 16) / 65536;
      }};
      // chỉ noise text-render path via measureText width ±0.01 — đủ unique, không vỡ layout
      const origMeasure = CanvasRenderingContext2D.prototype.measureText;
      CanvasRenderingContext2D.prototype.measureText = function(text) {{
        const m = origMeasure.call(this, text);
        try {{
          const w = m.width;
          Object.defineProperty(m, 'width', {{ get: () => w + (rnd() - 0.5) * 0.02, configurable: true }});
        }} catch (e) {{}}
        return m;
      }};
      CanvasRenderingContext2D.prototype.measureText.toString = () => 'function measureText() {{ [native code] }}';
    }} catch (e) {{}}

    // --- iframe.contentWindow.webdriver ---
    try {{
      const origAttach = Element.prototype.attachShadow;
      Element.prototype.attachShadow = function() {{
        const root = origAttach.apply(this, arguments);
        try {{
          if (root && root.window) {{
            Object.defineProperty(root.window, 'webdriver', {{ get: () => undefined }});
          }}
        }} catch (e) {{}}
        return root;
      }};
      Element.prototype.attachShadow.toString = () => 'function attachShadow() {{ [native code] }}';
    }} catch (e) {{}}

    // --- WebRTC: strip ICE servers (không leak IP thật khi có proxy) ---
    try {{
      if (window.RTCPeerConnection) {{
        const OrigRTC = window.RTCPeerConnection;
        const WrappedRTC = function(config, ...rest) {{
          if (config && config.iceServers) config.iceServers = [];
          return new OrigRTC(config, ...rest);
        }};
        WrappedRTC.prototype = OrigRTC.prototype;
        WrappedRTC.generateCertificate = OrigRTC.generateCertificate && OrigRTC.generateCertificate.bind(OrigRTC);
        window.RTCPeerConnection = WrappedRTC;
        if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = WrappedRTC;
        if (window.mozRTCPeerConnection) window.mozRTCPeerConnection = WrappedRTC;
      }}
    }} catch (e) {{}}

    // --- console.debug silence (some bots log from console) ---
    try {{
      const origDebug = console.debug;
      console.debug = function(...a) {{ /* silent */ }};
      console.debug.toString = () => 'function debug() {{ [native code] }}';
    }} catch (e) {{}}

    // --- performance.now entropy reduction (không cần quá chính xác) ---
    try {{
      if (window.performance && performance.now) {{
        const t0 = performance.now.bind(performance);
        performance.now = () => Math.round(t0() * 1000) / 1000;
      }}
    }} catch (e) {{}}
  }} catch (e) {{
    // fail silent — không crash page
  }}
}}"""


# =====================================================================
# LAUNCH ARGS — mạnh hơn anti_lock
# =====================================================================
def stealth_launch_args() -> list:
    args = list(base.stealth_launch_args())
    extra = [
        # chống automation fingerprint
        "--disable-blink-features=AutomationControlled",
        # không lộ extension / component
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-client-side-phishing-detection",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-first-run",
        "--no-default-browser-check",
        # fonts / media
        "--disable-ipc-flooding-protection",
        "--password-store=basic",
        "--use-mock-keychain",
        # network
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--disable-dev-shm-usage",
        # window
        "--start-maximized",
        "--autoplay-policy=no-user-gesture-required",
        # termux / android chromium hay cần
        "--disable-gpu-sandbox",
        "--ignore-certificate-errors",
        "--ignore-ssl-errors",
    ]
    # dedupe preserve order
    seen = set()
    out = []
    for a in args + extra:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


# =====================================================================
# apply_stealth — 1 entry point cho Playwright page
# =====================================================================
def apply_stealth(page, fp: dict | None = None, cookie_file: str | None = None) -> dict:
    """
    Áp stealth lên Playwright page:
      1. fp = get_or_create_fingerprint(cookie_file) nếu chưa có
      2. page.set_extra_http_headers UA (nếu set_user_agent chưa gọi)
      3. page.add_init_script(stealth JS)
      4. set_extra_http_headers Accept-Language
    Trả về fp dict (để caller set viewport/UA riêng nếu muốn).

    Usage:
        fp = apply_stealth(page, cookie_file="ck1.txt")
        browser.new_context(user_agent=fp["user_agent"], viewport=fp["viewport"], ...)
        # hoặc:
        page.goto(...)  # init script đã chạy
    """
    if fp is None:
        fp = get_or_create_fingerprint(cookie_file or "default.txt")

    js = build_stealth_js(fp)
    try:
        # Playwright: inject vào mọi frame + mọi navigation
        page.add_init_script(script=js)
    except Exception:
        # fallback: evaluate sau khi context sẵn sàng
        try:
            page.evaluate(js)
        except Exception:
            pass

    try:
        page.set_extra_http_headers({
            "Accept-Language": f"{fp.get('locale','en-US')},{fp.get('locale','en-US').split('-')[0]};q=0.9,en;q=0.8",
        })
    except Exception:
        pass

    # User-Agent qua CDP (đáng tin hơn header-only trên Chromium)
    try:
        client = page.context.new_cdp_session(page)
        client.send("Network.setUserAgentOverride", {
            "userAgent": fp.get("user_agent", ""),
            "acceptLanguage": fp.get("locale", "en-US"),
            "platform": fp.get("platform", "Win32"),
        })
        # client hints matching UA
        client.send("Emulation.setUserAgentOverride", {
            "userAgent": fp.get("user_agent", ""),
            "acceptLanguage": fp.get("locale", "en-US"),
            "platform": fp.get("platform", "Win32"),
            "userAgentMetadata": {
                "platform": fp.get("platform", "Win32").replace("Win32", "Windows"),
                "platformVersion": "15.0.0",
                "architecture": "x86",
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": False,
            },
        }).__class__  # some versions lack userAgentMetadata — ignore
    except Exception:
        try:
            client = page.context.new_cdp_session(page)
            client.send("Network.setUserAgentOverride", {
                "userAgent": fp.get("user_agent", ""),
                "acceptLanguage": fp.get("locale", "en-US"),
                "platform": fp.get("platform", "Win32"),
            })
        except Exception:
            pass

    # Spoof timezone via CDP (Playwright timezone_id cũng được)
    try:
        client2 = page.context.new_cdp_session(page)
        client2.send("Emulation.setTimezoneOverride", {
            "timezoneId": fp.get("timezone", "Asia/Ho_Chi_Minh"),
        })
    except Exception:
        pass

    return fp


# =====================================================================
# Human mouse — di chuyển không thẳng hàng (tránh bot pattern)
# =====================================================================
def human_move_to(page, x: int, y: int, steps: int | None = None):
    """Di chuyển chuột theo path cong (2 segment + jitter)."""
    if steps is None:
        steps = random.randint(12, 28)
    try:
        cur = page.mouse  # Playwright không expose pos; nhấp waypoint
        mid_x = int(x + random.uniform(-40, 40))
        mid_y = int(y + random.uniform(-30, 30))
        page.mouse.move(mid_x, mid_y, steps=max(4, steps // 2))
        page.mouse.move(int(x + random.uniform(-2, 2)), int(y + random.uniform(-2, 2)),
                        steps=max(3, steps - steps // 2))
    except Exception:
        try:
            page.mouse.move(x, y, steps=steps)
        except Exception:
            pass


def human_click(page, x: int, y: int, min_delay=0.08, max_delay=0.25):
    """Move + hover nhẹ + click trái (thỉnh thoảng click phải 0%)."""
    human_move_to(page, x, y)
    time.sleep(random.uniform(min_delay, max_delay))
    try:
        page.mouse.click(x, y, delay=random.randint(40, 120))
    except Exception:
        try:
            page.mouse.down()
            time.sleep(random.uniform(0.04, 0.1))
            page.mouse.up()
        except Exception:
            pass


# =====================================================================
# Self-test
# =====================================================================
if __name__ == "__main__":
    print("=== anti_lock_stealth.py self-test ===")
    fp = get_or_create_fingerprint("ck_stealth_test.txt")
    print("fingerprint:", json_dumps(fp) if (json_dumps := None) else "")
    import json as _j
    print(_j.dumps(fp, indent=2, ensure_ascii=False))
    js = build_stealth_js(fp)
    print(f"\nstealth JS length: {len(js)} chars (anti_lock base ~{len(base.build_stealth_js(fp))})")
    print("preview:", js[:180], "...")
    print(f"\nlaunch args: {len(stealth_launch_args())} (base {len(base.stealth_launch_args())})")
    ok, reason = check_daily_quota("ck_stealth_test.txt")
    print(f"quota ok={ok} {reason}")
    print("dest:", pick_next_dest("ck_stealth_test.txt"))
    print("apply_stealth signature ready (needs Playwright page)")
