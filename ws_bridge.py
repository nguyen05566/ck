"""
ws_bridge.py — Lớp dùng chung cho bot: chế độ riêng tư + bắt WebSocket.

Cung cấp:
  - launch_private_browser(): mỗi session 1 profile tạm, không dùng lại cache/cookie.
  - close_private_browser(): đóng browser và xoá sạch profile tạm (xoá lịch sử).
  - wipe_storage(): xoá cache HTTP, cookie, localStorage, IndexedDB, service worker.
  - install_ws_hook(): chặn WebSocket để ghi lại toàn bộ frame gửi/nhận.
  - capture_frames()/send_raw()/replay_frame(): đọc frame đã bắt và gửi frame thô.

Mô hình theo arena10k.py (vht): đăng nhập xong bắt socket, đọc/gửi frame trực tiếp.
"""
import base64
import os
import shutil
import tempfile

WS_HOOK_JS = r"""
(() => {
  if (window.__wsHookInstalled) return;
  window.__wsHookInstalled = true;

  const Native = window.WebSocket;
  const cap = (window.__ws_capture = { frames: [], cmds: [], max: 6000 });
  const socks = (window.__ws_sockets = []);

  const b64 = (bytes) => {
    let s = '';
    const n = bytes.length;
    const CH = 0x8000;
    for (let i = 0; i < n; i += CH) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    }
    return btoa(s);
  };

  const encode = (d) => {
    try {
      if (typeof d === 'string') return { t: 'text', v: d };
      if (d instanceof ArrayBuffer) return { t: 'bin', v: b64(new Uint8Array(d)) };
      if (ArrayBuffer.isView(d)) {
        return { t: 'bin', v: b64(new Uint8Array(d.buffer, d.byteOffset, d.byteLength)) };
      }
      if (d && d.buffer instanceof ArrayBuffer) {
        return { t: 'bin', v: b64(new Uint8Array(d.buffer)) };
      }
    } catch (e) { /* ignore */ }
    return { t: 'unknown', v: String(d) };
  };

  const push = (sock, dir, data) => {
    const e = encode(data);
    cap.frames.push({
      t: Date.now(),
      dir: dir,
      url: sock.__wsUrl || '',
      type: e.t,
      len: e.v ? e.v.length : 0,
      data: e.v,
    });
    if (cap.frames.length > cap.max) {
      cap.frames.splice(0, cap.frames.length - cap.max);
    }
  };

  function HookedWS(url, protocols) {
    const sock = protocols === undefined ? new Native(url) : new Native(url, protocols);
    sock.__wsUrl = String(url);
    socks.push(sock);
    const origSend = sock.send.bind(sock);
    sock.send = function (data) { push(sock, 'out', data); return origSend(data); };
    sock.addEventListener('message', function (ev) { push(sock, 'in', ev.data); });
    return sock;
  }

  HookedWS.prototype = Native.prototype;
  HookedWS.CONNECTING = Native.CONNECTING;
  HookedWS.OPEN = Native.OPEN;
  HookedWS.CLOSING = Native.CLOSING;
  HookedWS.CLOSED = Native.CLOSED;
  window.WebSocket = HookedWS;

  window.__ws_sendRaw = function (b64data, idx) {
    const sock = socks[idx || 0];
    if (!sock) return { ok: false, error: 'no socket' };
    try {
      const bin = atob(b64data);
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      sock.send(arr);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  };

  window.__ws_socketInfo = function () {
    return socks.map((s, i) => ({ i: i, url: s.__wsUrl, readyState: s.readyState }));
  };

  const hookConn = function (c) {
    if (!c || typeof c.send !== 'function') return false;
    if (c.__wsHooked) return true;
    c.__wsHooked = true;
    const orig = c.send.bind(c);
    c.send = function (msg, cb) {
      try {
        let name = '';
        let keys = [];
        if (msg) {
          name = msg.name || msg.command || msg.cmd || msg.type || msg.msgType || '';
          if (!name && msg.constructor) name = msg.constructor.name || '';
          keys = Object.keys(msg);
        }
        cap.cmds.push({ t: Date.now(), name: String(name), keys: keys });
      } catch (e) { /* ignore */ }
      return orig(msg, cb);
    };
    return true;
  };

  let connRef = window.connection;
  try {
    Object.defineProperty(window, 'connection', {
      configurable: true,
      enumerable: true,
      get: function () { return connRef; },
      set: function (v) { connRef = v; hookConn(v); },
    });
  } catch (e) { /* ignore */ }
  hookConn(connRef);

  let tries = 0;
  const timer = setInterval(function () {
    tries += 1;
    if (hookConn(window.connection) || tries > 600) clearInterval(timer);
  }, 100);
})();
"""

JS_WIPE = r"""
() => {
  try { localStorage.clear(); } catch (e) {}
  try { sessionStorage.clear(); } catch (e) {}
  try {
    if (window.caches && caches.keys) {
      caches.keys().then((ks) => ks.forEach((k) => caches.delete(k))).catch(() => {});
    }
  } catch (e) {}
  try {
    if (window.indexedDB && indexedDB.databases) {
      indexedDB.databases().then((dbs) => {
        (dbs || []).forEach((d) => { if (d.name) indexedDB.deleteDatabase(d.name); });
      }).catch(() => {});
    }
  } catch (e) {}
  try {
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      navigator.serviceWorker.getRegistrations().then((rs) => rs.forEach((r) => r.unregister())).catch(() => {});
    }
  } catch (e) {}
  return true;
}
"""

DEFAULT_ORIGINS = (
    "https://www.facebook.com",
    "https://facebook.com",
    "https://www.fbsbx.com",
    "https://fbsbx.com",
)


def install_ws_hook(context):
    """Bật chặn WebSocket cho mọi frame/page mới trong context."""
    try:
        context.add_init_script(WS_HOOK_JS)
        return True
    except Exception:
        return False


def launch_private_context(playwright, headless=True, args=None,
                           context_kwargs=None, launch_kwargs=None):
    """Mở context riêng tư trên profile tạm (không dùng lại cache/cookie/lịch sử).

    Trả về (context, profile_dir). profile_dir phải xoá bằng close_private_context.
    """
    profile_dir = tempfile.mkdtemp(prefix="botpriv-")
    kwargs = {
        "user_data_dir": profile_dir,
        "headless": headless,
        "args": args or ["--no-sandbox", "--disable-dev-shm-usage",
                         "--disable-blink-features=AutomationControlled"],
    }
    chrome_path = os.environ.get("BOT_CHROME_PATH", "").strip()
    if chrome_path:
        kwargs["executable_path"] = chrome_path
    if context_kwargs:
        kwargs.update(context_kwargs)
    if launch_kwargs:
        kwargs.update(launch_kwargs)
    context = playwright.chromium.launch_persistent_context(**kwargs)
    return context, profile_dir


def close_private_context(context, profile_dir):
    """Đóng context và xoá profile tạm (xoá cache + lịch sử trên đĩa)."""
    try:
        if context:
            context.close()
    except Exception:
        pass
    if profile_dir:
        shutil.rmtree(profile_dir, ignore_errors=True)


def wipe_storage(context, page=None, origins=DEFAULT_ORIGINS):
    """Xoá cache, cookie, storage, service worker, lịch sử của origin.

    Gọi TRƯỚC khi nạp cookie đăng nhập và SAU khi kết thúc session.
    """
    result = {"cache": False, "cookies": False, "storage": False, "js": False}
    try:
        cdp = context.new_cdp_session(page) if page else None
    except Exception:
        cdp = None
    if cdp:
        for method, key in (("Network.clearBrowserCache", "cache"),
                            ("Network.clearBrowserCookies", "cookies")):
            try:
                cdp.send(method)
                result[key] = True
            except Exception:
                pass
        for origin in origins:
            try:
                cdp.send("Storage.clearDataForOrigin",
                         {"origin": origin, "storageTypes": "all"})
                result["storage"] = True
            except Exception:
                pass
        try:
            cdp.detach()
        except Exception:
            pass
    try:
        context.clear_cookies()
        result["cookies"] = True
    except Exception:
        pass
    if page:
        try:
            page.evaluate(JS_WIPE)
            result["js"] = True
        except Exception:
            pass
    return result


def capture_frames(page, limit=0):
    """Trả về list frame đã bắt (mỗi frame: t, dir, url, type, len, data)."""
    try:
        frames = page.evaluate("() => (window.__ws_capture && window.__ws_capture.frames) || []")
    except Exception:
        return []
    if limit and len(frames) > limit:
        frames = frames[-limit:]
    return frames


def capture_commands(page):
    """Trả về list lệnh game đã gửi qua connection.send (tên lệnh + khóa)."""
    try:
        return page.evaluate("() => (window.__ws_capture && window.__ws_capture.cmds) || []")
    except Exception:
        return []


def capture_all(page):
    """Gộp frame + lệnh, đánh dấu frame 'out' theo thứ tự lệnh gửi đi."""
    frames = capture_frames(page)
    cmds = capture_commands(page)
    out_idx = 0
    for fr in frames:
        if fr.get("dir") != "out":
            continue
        cmd = None
        if out_idx < len(cmds):
            cmd = cmds[out_idx].get("name") or ""
        fr["cmd"] = cmd
        out_idx += 1
    return frames, cmds


def socket_info(page):
    try:
        return page.evaluate("() => window.__ws_socketInfo ? window.__ws_socketInfo() : []")
    except Exception:
        return []


def send_raw(page, payload, sock_index=0):
    """Gửi frame nhị phân thô lên socket đã bắt (data: bytes)."""
    b64 = base64.b64encode(payload).decode("ascii")
    try:
        return page.evaluate(
            "([d, i]) => window.__ws_sendRaw ? window.__ws_sendRaw(d, i) : {ok:false,error:'no hook'}",
            [b64, sock_index])
    except Exception as e:
        return {"ok": False, "error": str(e)}


def replay_frame(page, frame_index, sock_index=0):
    """Gửi lại một frame 'out' đã bắt (dùng để test lệnh thô trên đúng socket)."""
    frames = capture_frames(page)
    outs = [f for f in frames if f.get("dir") == "out"]
    if frame_index < 0 or frame_index >= len(outs):
        return {"ok": False, "error": f"index out of range (0..{len(outs) - 1})"}
    f = outs[frame_index]
    if f.get("type") != "bin":
        return {"ok": False, "error": "frame is not binary"}
    try:
        payload = base64.b64decode(f["data"])
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return send_raw(page, payload, sock_index)


def dump_frames(frames, out_dir="."):
    """Lưu frame bắt được ra file (json + nhị phân thô) để phân tích protocol."""
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "ws_frames.json")
    with open(json_path, "w", encoding="utf-8") as f:
        import json
        json.dump(frames, f, ensure_ascii=False, indent=1)
    raw_path = os.path.join(out_dir, "ws_frames.bin")
    with open(raw_path, "wb") as f:
        for fr in frames:
            if fr.get("type") != "bin" or not fr.get("data"):
                continue
            try:
                f.write(base64.b64decode(fr["data"]))
            except Exception:
                pass
    return json_path, raw_path
