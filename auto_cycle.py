#!/usr/bin/env python3
"""
auto_cycle.py — Combined claim + surrender cycle for Gomoku.

Flow:
  Loop:
    1. Claim reward (ck_new.py logic) until balance reaches [50k, 55k] range
       - Stops early if server returns "no response" (claim rejected)
       - Stops at 55k to avoid game's claim block kicking in too early
    2. Find table by target_name in Gomoku lobby
    3. Join table + send SURRENDER → lose bet amount to target
    4. Balance drops below 50k → can claim again
    5. Repeat

Why 50k-55k?
  - Server blocks claim when balance >= ~50k
  - But we want to safely reach 50k+ before surrender
  - Stopping at 55k gives a buffer so the surrender bet (40k) leaves us ~10-15k
    which is below 50k → next cycle's claim works
  - If surrender bet is 40k and we have 55k → after surrender: 15k (good, < 50k)

Usage:
  python3 auto_cycle.py \\
      --cookie ck.txt \\
      --target-name "nguyen" \\
      --target-id 68639698 \\
      --cycles 3 \\
      --claim-min 50000 \\
      --claim-max 55000 \\
      --bet-max 100000

  Or via env vars:
  SINGLE_COOKIE_FILE=ck.txt TARGET_NAME=nguyen TARGET_ID=68639698 \\
  CYCLES=3 python3 auto_cycle.py
"""
import argparse
import os
import re
import sys
import time
import random
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import anti_lock as al

from playwright.sync_api import sync_playwright

GAME_URL = "https://www.facebook.com/gaming/play/gomoku-vh"


def parse_cookie_string(raw):
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        n, v = part.split("=", 1)
        out.append({"name": n.strip(), "value": v.strip(),
                    "domain": ".facebook.com", "path": "/",
                    "secure": True, "httpOnly": False, "sameSite": "Lax"})
    return out


def parse_bet(s):
    if not s or s == "?":
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


def log(msg=""):
    print(msg, flush=True)


# ---- JS helpers ----
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
    path: window.place ? place.thisPath : null,
    balance: document.querySelector('.chipBalance')?.textContent || null,
    playerId: window.currentPlayerId || null,
    body: document.body ? document.body.innerText.slice(0, 600) : '',
})"""

# JS_CLAIM — matches ck_new.py's JS_CLAIM (simple, no v3 patch)
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

JS_CLOSE_DIALOGS = """() => {
    try { document.querySelectorAll('.msgBoxBackGround,.msgBox').forEach(el => el.remove()); return true; }
    catch(e) { return false; }
}"""

# trigger_and_claim — same logic as ck_new.py
JS_CREATE_TABLE = """() => { try { createTable(); } catch(e) {} }"""

JS_SELECT_RADIO_11 = """() => {
    const r = document.getElementById('radio_11');
    if (r) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles:true})); }
}"""

JS_CLICK_CREATE = """() => {
    const b = document.querySelector('input[name="CREATE"]');
    if (b) b.click();
}"""

# Gomoku UI navigation (for surrender phase)
JS_CLICK_CHON_BAN = """() => {
    for (const b of document.querySelectorAll('a, button')) {
        if ((b.innerText || '').trim() === 'Chọn bàn' && b.offsetParent !== null) { b.click(); return true; }
    }
    return false;
}"""

JS_CLICK_FILTER_TAT_CA = """() => {
    for (const b of document.querySelectorAll('a, button')) {
        if (b.id === 'table_filter0' && b.offsetParent !== null) { b.click(); return true; }
    }
    return false;
}"""

JS_CLICK_FILTER_CON_TRONG = """() => {
    for (const b of document.querySelectorAll('a, button')) {
        if (b.id === 'table_filter1' && b.offsetParent !== null) { b.click(); return true; }
    }
    return false;
}"""

JS_FIND_TABLE_BY_NAME = """(playerName) => {
    const matches = [];
    document.querySelectorAll('div[id^=table_]').forEach(t => {
        if (t.offsetParent === null) return;
        const r = t.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return;
        const text = (t.innerText || '').trim();
        const parts = text.split('\\n');
        const id = parseInt(t.id.replace('table_', ''));
        if (isNaN(id)) return;
        const cls = (t.className || '').toString();
        const all_text = text.toLowerCase();
        if (all_text.includes(playerName.toLowerCase())) {
            matches.push({
                id, bet: parts[0] || '', player: parts[1] || '', cls,
                has_exceed: cls.includes('exceed')
            });
        }
    });
    return matches;
}"""

# Close the table-info popup (after clicking a table to look at player IDs)
JS_CLOSE_TABLE_POPUP = """() => {
    const closeBtn = document.querySelector('.msgBox .closeButton');
    if (closeBtn) { closeBtn.click(); return true; }
    return false;
}"""

# Get all showGameProfile(playerId) elements currently visible in popups.
# Returns array of {player_id, text} for each profile link.
JS_GET_VISIBLE_PROFILES = """() => {
    const out = [];
    document.querySelectorAll('[onclick*=showGameProfile]').forEach(el => {
        if (el.offsetParent === null) return;
        const onclick = el.getAttribute('onclick') || '';
        const m = onclick.match(/showGameProfile\\((\\d+)\\)/);
        if (m) {
            out.push({player_id: parseInt(m[1]), text: (el.textContent || '').trim().slice(0, 80)});
        }
    });
    return out;
}"""

# === WS tap approach (faster than DOM clicking) ===
# When we click a table, server sends PLAYER_ENTERED (cmd 0x019E) message via WS
# containing player_ids of all players in that table. We tap WS messages and
# search for target_id in the binary data — no DOM parsing needed.

JS_INSTALL_WS_TAP = """() => {
    if (window.__tap_installed) return true;
    window.__tap = [];
    window.__last_tap_count = 0;
    const ws = window.connection && window.connection.ws;
    if (!ws) return false;
    const orig = ws.onmessage;
    ws.binaryType = 'arraybuffer';
    ws.onmessage = function(ev) {
        let bytes = null;
        try {
            if (ev.data instanceof ArrayBuffer) bytes = Array.from(new Uint8Array(ev.data));
            else if (ev.data && ev.data.data) bytes = Array.from(new Uint8Array(ev.data.data));
        } catch(e) {}
        if (bytes) window.__tap.push({ts: Date.now(), bytes});
        if (orig) return orig.call(ws, ev);
    };
    window.__tap_installed = true;
    return true;
}"""

JS_CLEAR_TAP = "() => { window.__tap = []; window.__last_tap_count = 0; return true; }"

JS_GET_TAP = "() => window.__tap || []"

# Scan ALL tables (id, bet, player_name) without name filter
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

JS_GO_LOBBY = """() => {
    try {
        if (typeof place.enterParent === 'function') { place.enterParent(); return 'enterParent'; }
        if (typeof place.leaveTable === 'function') { place.leaveTable(); return 'leaveTable'; }
        return 'no leave function';
    } catch(e) { return 'err: ' + e.toString(); }
}"""


def find_game_frame(page, max_wait=60):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        for f in page.frames:
            try:
                if f.is_detached():
                    continue
                ok = f.evaluate(
                    "() => !!(window.connection || document.querySelector('.chipBalance') || typeof window.createTable === 'function')"
                )
                if ok:
                    return f
            except Exception:
                continue
        time.sleep(2)
    return None


def get_balance(game_frame):
    s = game_frame.evaluate(JS_GET_STATE)
    return parse_bet(s.get("balance")), s


def ensure_in_lobby(game_frame, page, max_attempts=3):
    """Make sure we're back at Lobby (not in a table)."""
    for _ in range(max_attempts):
        s = game_frame.evaluate(JS_GET_STATE)
        if s.get("path") == "Lobby":
            return True
        log(f"  [nav] path={s.get('path')} — leaving table...")
        try:
            game_frame.evaluate(JS_GO_LOBBY)
        except Exception as e:
            log(f"  [nav] leave err: {type(e).__name__}")
        time.sleep(2)
        new_gf = find_game_frame(page, max_wait=15)
        if new_gf:
            game_frame = new_gf
    s = game_frame.evaluate(JS_GET_STATE)
    return s.get("path") == "Lobby"


def trigger_and_claim(game_frame):
    """Send VIDEO_REWARD claim — matches ck_new.py's trigger_and_claim()."""
    # Create table (triggers "enough coin" alert)
    game_frame.evaluate(JS_CREATE_TABLE)
    time.sleep(2)
    game_frame.evaluate(JS_SELECT_RADIO_11)
    time.sleep(0.5)
    game_frame.evaluate(JS_CLICK_CREATE)
    time.sleep(3)

    # Click "Watch" button if alert shows
    game_frame.evaluate(JS_CLICK_WATCH)
    time.sleep(1)

    # Send VIDEO_REWARD
    result = game_frame.evaluate(JS_CLAIM, 15000)
    if not isinstance(result, dict):
        result = {"success": False, "error": "bad result"}
    return result


def claim_until_threshold(game_frame, page, claim_min, claim_max, max_claims=200, cooldown=1.0):
    """Claim VIDEO_REWARD repeatedly until balance is in [claim_min, claim_max].
    Returns (final_balance, num_claims_ok, num_claims_fail, server_rejected)."""
    log(f"\n[CLAIM] claiming until balance in [{claim_min:,}, {claim_max:,}] (max {max_claims} claims)...")
    claims_ok = 0
    claims_fail = 0
    consec_fail = 0
    server_rejected = False

    for i in range(max_claims):
        # Close popups + watch button
        game_frame.evaluate(JS_CLOSE_POPUPS)
        game_frame.evaluate(JS_CLICK_WATCH)
        time.sleep(0.3)
        game_frame.evaluate(JS_CLOSE_DIALOGS)
        time.sleep(0.3)

        # Check balance
        bal, _ = get_balance(game_frame)
        if bal >= claim_min:
            log(f"  [t={i}] balance {bal:,} ≥ {claim_min:,} ✓ stopping claim loop")
            break
        if bal >= claim_max:
            log(f"  [t={i}] balance {bal:,} ≥ {claim_max:,} (max reached) ✓ stopping")
            break

        # Send claim
        result = trigger_and_claim(game_frame)

        if result.get("success") and result.get("amount", 0) > 0:
            amount = result["amount"]
            claims_ok += 1
            consec_fail = 0
            bal_after, _ = get_balance(game_frame)
            log(f"  [t={i}] ✓ claim +{amount} | {bal:,} → {bal_after:,}")
        else:
            claims_fail += 1
            consec_fail += 1
            err = result.get("error", "?")
            log(f"  [t={i}] ✗ claim FAIL ({err}) | consec={consec_fail}")
            # "no response" is the server's signal that claim is blocked (balance too high or quota)
            if err == "no response":
                server_rejected = True
                log(f"  [stop] server rejected claim (likely balance too high or quota)")
                # Re-check balance — may have hit >50k without us realizing
                bal, _ = get_balance(game_frame)
                if bal >= claim_min:
                    log(f"  balance {bal:,} ≥ {claim_min:,} — OK to proceed to surrender")
                break
            if consec_fail >= 6:
                log(f"  [stop] 6 consecutive failures — likely exhausted")
                break

        # Human-like delay between claims
        time.sleep(cooldown + random.uniform(0.5, 1.5))

    bal, _ = get_balance(game_frame)
    log(f"[CLAIM done] {claims_ok} OK / {claims_fail} fail | final balance: {bal:,}")
    return bal, claims_ok, claims_fail, server_rejected


def open_table_browser(game_frame, page, filter_name="Tất cả"):
    """Click 'Chọn bàn' → filter (Tất cả / Còn trống)."""
    s = game_frame.evaluate(JS_GET_STATE)
    if s.get("path") != "Lobby":
        ensure_in_lobby(game_frame, page)
    game_frame.evaluate(JS_CLOSE_POPUPS)
    time.sleep(0.5)
    game_frame.evaluate(JS_CLICK_CHON_BAN)
    time.sleep(5)
    if filter_name == "Tất cả":
        game_frame.evaluate(JS_CLICK_FILTER_TAT_CA)
    elif filter_name == "Còn trống":
        game_frame.evaluate(JS_CLICK_FILTER_CON_TRONG)
    time.sleep(2)


def find_target_table(game_frame, target_name, bet_max):
    """Find a table hosted by target_name. Returns target table dict or None."""
    matches = game_frame.evaluate(JS_FIND_TABLE_BY_NAME, target_name)
    if not matches:
        return None
    # Sort by bet ascending
    matches.sort(key=lambda t: parse_bet(t.get("bet", "999999")))

    # First try: tables we can afford (no "exceed") AND bet <= bet_max
    joinable = [t for t in matches if not t.get("has_exceed") and parse_bet(t.get("bet", "999999")) <= bet_max]
    if joinable:
        return joinable[0]

    # Second try: tables with bet <= bet_max (even if "exceed" — we'll attempt anyway)
    by_bet = [t for t in matches if parse_bet(t.get("bet", "999999")) <= bet_max]
    if by_bet:
        log(f"  [scan] target found but all tables have 'exceed' (balance < bet):")
        for t in by_bet:
            log(f"    id={t['id']} bet={t['bet']} cls={t['cls']}")
        return by_bet[0]

    log(f"  [scan] found {len(matches)} tables for '{target_name}' but none with bet <= {bet_max}")
    for t in matches:
        log(f"    id={t['id']} bet={t['bet']} cls={t['cls']}")
    return None


def find_target_by_id(game_frame, target_id, bet_max, max_tables_to_scan=30):
    """Find a table where target_id is one of the players.
    Uses WS tap approach (faster than DOM popup parsing):
      1. Install WS message tap (if not installed yet)
      2. For each table: clear tap, click table, check WS messages for target_id bytes
      3. If found, return table dict

    This works because when we click a table, server sends PLAYER_ENTERED (0x019E)
    message containing player_ids of all players in that table. We search the
    binary data for target_id (4-byte BE int32) — no DOM parsing needed.
    """
    import struct
    target_be = struct.pack('>I', int(target_id))
    target_bytes_list = list(target_be)

    # Install WS tap (idempotent — if already installed, returns true)
    game_frame.evaluate(JS_INSTALL_WS_TAP)

    # Scan all tables visible in lobby
    tables = game_frame.evaluate(JS_SCAN_ALL_TABLES)
    if not tables:
        return None

    log(f"  [scan-by-id] {len(tables)} tables visible — clicking each, checking WS for target_id={target_id}")
    tables_to_scan = tables[:max_tables_to_scan]

    found_table = None
    for t in tables_to_scan:
        # Close any existing popup
        game_frame.evaluate(JS_CLOSE_TABLE_POPUP)
        time.sleep(0.1)
        game_frame.evaluate(JS_CLEAR_TAP)
        game_frame.evaluate(JS_CLICK_TABLE, t["id"])
        time.sleep(0.3)  # wait for PLAYER_ENTERED to arrive
        # Check tap messages for target_id
        tap = game_frame.evaluate(JS_GET_TAP)
        for msg in tap:
            b = msg.get('bytes') or []
            for off in range(len(b) - 3):
                if b[off:off+4] == target_bytes_list:
                    log(f"  ✓✓✓ FOUND target_id={target_id} at table {t['id']} (player: '{t['player']}', bet: {t['bet']})")
                    found_table = t
                    break
            if found_table:
                break
        if found_table:
            break

    # Close any popup before returning
    game_frame.evaluate(JS_CLOSE_TABLE_POPUP)
    time.sleep(0.3)

    if not found_table:
        log(f"  [scan-by-id] target_id={target_id} not found in any of the {len(tables_to_scan)} tables scanned")
        return None

    # Filter by bet_max
    bet_val = parse_bet(found_table.get("bet", "999999"))
    if bet_val > bet_max:
        log(f"  [scan-by-id] found but bet {bet_val:,} > bet_max {bet_max:,} — skipping")
        return None

    return found_table


def wait_for_target_with_retry(game_frame, page, target_name, target_id, bet_max, max_wait_seconds=60, max_tables_to_scan=30):
    """Open table browser + poll for target's table up to max_wait_seconds.
    Strategy:
      - If target_id is set (>0), search by ID (click each table to find player IDs)
      - Else if target_name is set (non-empty), search by name
      - Else error
    Returns target table dict or None if not found within the time budget.
    """
    use_id = target_id and int(target_id) > 0
    use_name = bool(target_name and target_name.strip())

    if not use_id and not use_name:
        log("  [wait] neither target_name nor target_id set — cannot find target")
        return None

    search_mode = "ID" if use_id else "name"
    log(f"  [wait] polling for target (mode={search_mode}, target_id={target_id}, name={target_name!r}) up to {max_wait_seconds}s...")
    deadline = time.time() + max_wait_seconds
    attempt = 0
    # Open browser first with "Tất cả" filter
    open_table_browser(game_frame, page, filter_name="Tất cả")

    while time.time() < deadline:
        attempt += 1
        if use_id:
            target = find_target_by_id(game_frame, int(target_id), bet_max, max_tables_to_scan=max_tables_to_scan)
        else:
            target = find_target_table(game_frame, target_name, bet_max)
        if target:
            return target

        # Switch to "Còn trống" filter as alternative
        game_frame.evaluate(JS_CLICK_FILTER_CON_TRONG)
        time.sleep(1)
        if use_id:
            target = find_target_by_id(game_frame, int(target_id), bet_max, max_tables_to_scan=max_tables_to_scan)
        else:
            target = find_target_table(game_frame, target_name, bet_max)
        if target:
            return target

        # Switch back to "Tất cả"
        game_frame.evaluate(JS_CLICK_FILTER_TAT_CA)
        # Wait + try again (every 10s)
        wait = min(5, max(1, deadline - time.time()))
        log(f"  [wait] attempt #{attempt} (mode={search_mode}): target not found — retry in {wait:.0f}s...")
        time.sleep(wait)
        # Re-open table browser (in case lobby state changed)
        try:
            s = game_frame.evaluate(JS_GET_STATE)
            if s.get("path") != "Lobby":
                ensure_in_lobby(game_frame, page)
            open_table_browser(game_frame, page, filter_name="Tất cả")
        except Exception as e:
            log(f"  [wait] re-open lobby err: {e}")
    return None


def surrender_one_round(game_frame, page, target_name, target_id, bet_max, target_wait_seconds=60, max_tables_to_scan=30):
    """Find target table → join → SURRENDER. Returns (success, balance_lost).
    If target_id > 0, search by ID (preferred). Else search by name.
    """
    log("\n[SURRENDER] starting surrender round...")
    bal_before, _ = get_balance(game_frame)
    log(f"  balance before: {bal_before:,}")

    # Step 1+2: poll for target's table with retry (waits up to target_wait_seconds)
    log(f"  [1+2] looking for target (id={target_id}, name={target_name!r}) up to {target_wait_seconds}s...")
    target = wait_for_target_with_retry(game_frame, page, target_name, target_id, bet_max,
                                         max_wait_seconds=target_wait_seconds,
                                         max_tables_to_scan=max_tables_to_scan)
    if not target:
        log(f"    [!] target not found within {target_wait_seconds}s — skipping surrender")
        log(f"    [hint] make sure target has created a table with bet <= {bet_max} xu")
        return False, 0

    log(f"    ✓ found: table {target['id']} bet={target['bet']} player={target['player']}")

    # Close any popup before clicking the table again
    game_frame.evaluate(JS_CLOSE_TABLE_POPUP)
    time.sleep(0.5)

    # Step 3: click on target table
    log(f"  [3] clicking table {target['id']}...")
    game_frame.evaluate(JS_CLICK_TABLE, target["id"])
    time.sleep(5)

    # Step 4: click "Vào chơi"
    log("  [4] looking for 'Vào chơi' button...")
    clicked_vao_choi = False
    for i in range(10):
        r = game_frame.evaluate(JS_CLICK_VAO_CHOI)
        if r.get("clicked"):
            log(f"    ✓ clicked Vào chơi")
            clicked_vao_choi = True
            break
        time.sleep(0.5)
    if not clicked_vao_choi:
        log("    [!] Vào chơi not found — table may be full or rejected")
        return False, 0

    # Step 5: wait for "Bắt đầu" button (or game may have auto-started)
    log("  [5] waiting for Bắt đầu button (max 30s)...")
    found_start = False
    for i in range(15):
        r = game_frame.evaluate(JS_FIND_BAT_DAU)
        if r.get("found"):
            log(f"    ✓ Bắt đầu found (text: {r.get('text')})")
            game_frame.evaluate(JS_CLICK_BAT_DAU)
            log(f"    → clicked Bắt đầu, waiting 5s for game start...")
            time.sleep(5)
            found_start = True
            break
        time.sleep(2)
    if not found_start:
        log("    [!] Bắt đầu not found — trying SURRENDER anyway")

    # Step 6: send SURRENDER
    log("  [6] sending SURRENDER...")
    r = game_frame.evaluate(JS_SEND_SURRENDER)
    log(f"    result: {r}")
    surrender_ok = r.get("ok", False) or r.get("err_code") == 22

    # If "Bạn không tham gia bàn chơi này" — wait + retry
    if not surrender_ok and r.get("err_msg") and "không tham gia" in r.get("err_msg", ""):
        log("    [!] not a player yet — waiting 10s + retry...")
        time.sleep(10)
        r = game_frame.evaluate(JS_SEND_SURRENDER)
        log(f"    retry result: {r}")
        surrender_ok = r.get("ok", False) or r.get("err_code") == 22

    # Step 7: close game-over dialog + return to lobby
    log("  [7] closing dialog + returning to lobby...")
    time.sleep(3)
    for _ in range(5):
        clicked = game_frame.evaluate(JS_CLICK_OK)
        if clicked.get("clicked"):
            log(f"    closed dialog: {clicked}")
            time.sleep(1)
            break
        time.sleep(1)

    ensure_in_lobby(game_frame, page)

    # Final balance
    bal_after, _ = get_balance(game_frame)
    delta = bal_after - bal_before
    log(f"  [done] balance: {bal_before:,} → {bal_after:,} (Δ={delta:+,})")
    if delta < 0:
        log(f"         ✓ lost {-delta:,} xu (transferred to target)")
    return surrender_ok, max(0, -delta)


def main():
    parser = argparse.ArgumentParser(description="Gomoku auto-cycle: claim → surrender → claim → surrender ...")
    parser.add_argument("--cookie", default=os.environ.get("SINGLE_COOKIE_FILE", "ck.txt"),
                        help="Cookie file (default: ck.txt)")
    parser.add_argument("--target-name", default=os.environ.get("TARGET_NAME", "nguyen"),
                        help="Player name to find in tables (optional — if target_id is set, name is ignored)")
    parser.add_argument("--target-id", type=int, default=int(os.environ.get("TARGET_ID", "68639698")),
                        help="Player ID to find (preferred — unique, no name collision). Default: 0 (search by name)")
    parser.add_argument("--max-tables-to-scan", type=int, default=int(os.environ.get("MAX_TABLES_TO_SCAN", "30")),
                        help="Max tables to scan when searching by ID (default: 30 — each table click takes ~1.5s)")
    parser.add_argument("--cycles", type=int, default=int(os.environ.get("CYCLES", "3")),
                        help="Number of claim→surrender cycles (default: 3)")
    parser.add_argument("--claim-min", type=int, default=int(os.environ.get("CLAIM_MIN", "40000")),
                        help="Stop claiming when balance >= this (default: 40000)")
    parser.add_argument("--claim-max", type=int, default=int(os.environ.get("CLAIM_MAX", "45000")),
                        help="Hard stop claiming when balance >= this (default: 45000)")
    parser.add_argument("--bet-max", type=int, default=int(os.environ.get("BET_MAX", "100000")),
                        help="Max bet to accept (default: 100000)")
    parser.add_argument("--min-balance", type=int, default=int(os.environ.get("MIN_BALANCE", "1000")),
                        help="Stop if balance drops below this (default: 1000)")
    parser.add_argument("--max-claims-per-cycle", type=int, default=int(os.environ.get("MAX_CLAIMS_PER_CYCLE", "200")),
                        help="Max claim attempts per cycle (default: 200)")
    parser.add_argument("--claim-cooldown", type=float, default=float(os.environ.get("CLAIM_COOLDOWN", "1.0")),
                        help="Seconds between claims (default: 1.0)")
    parser.add_argument("--delay-between-cycles", type=float, default=float(os.environ.get("DELAY_BETWEEN_CYCLES", "10")),
                        help="Seconds between cycles (default: 10)")
    parser.add_argument("--target-wait-seconds", type=int, default=int(os.environ.get("TARGET_WAIT_SECONDS", "60")),
                        help="Seconds to wait for target's table to appear (default: 60)")
    parser.add_argument("--headless", default=os.environ.get("HEADLESS", "true").lower() == "true",
                        action="store_true", help="Run headless")
    args = parser.parse_args()

    cookie_path = args.cookie
    if not os.path.isabs(cookie_path):
        cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), cookie_path)
    if not os.path.exists(cookie_path):
        log(f"[ERR] cookie file not found: {cookie_path}")
        return 1

    cookie_raw = open(cookie_path, encoding="utf-8").read().strip()
    cookies = parse_cookie_string(cookie_raw)
    log(f"[cookie] parsed {len(cookies)} cookies from {os.path.basename(cookie_path)}")

    fp = al.get_or_create_fingerprint(os.path.basename(cookie_path))
    log(f"[fp] UA={fp['user_agent'][:50]}... viewport={fp['viewport']}")
    log(f"[config] target='{args.target_name}' (id={args.target_id})")
    log(f"         claim_min={args.claim_min:,}, claim_max={args.claim_max:,}")
    log(f"         bet_max={args.bet_max:,}, cycles={args.cycles}")

    cycle_results = []
    total_xu_to_target = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, args=al.stealth_launch_args(fp))
        ctx = browser.new_context(
            viewport=fp["viewport"], locale=fp["locale"],
            user_agent=fp["user_agent"], timezone_id=fp["timezone"],
            color_scheme=fp.get("color_scheme", "light"),
        )
        ctx.add_init_script(al.build_stealth_js(fp))
        ctx.add_cookies([dict(c, domain=".facebook.com") for c in cookies])
        page = ctx.new_page()
        page.set_default_timeout(45000)

        log(f"\n[setup] goto FB home → {GAME_URL}")
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        game_frame = find_game_frame(page, max_wait=60)
        if not game_frame:
            log("[ERR] no game frame after 60s")
            browser.close()
            return 1
        log(f"[setup] game frame ready")

        # Close any startup popups
        game_frame.evaluate(JS_CLOSE_POPUPS)
        time.sleep(2)

        bal, state = get_balance(game_frame)
        log(f"\n[init] balance: {bal:,}, player_id: {state.get('playerId')}, path: {state.get('path')}")

        for cycle_num in range(1, args.cycles + 1):
            log(f"\n{'#' * 60}")
            log(f"# CYCLE {cycle_num}/{args.cycles}")
            log(f"{'#' * 60}")

            # Check min balance
            bal, _ = get_balance(game_frame)
            if bal < args.min_balance:
                log(f"[STOP] balance {bal:,} < min {args.min_balance:,}")
                break

            # Ensure we're in Lobby
            if not ensure_in_lobby(game_frame, page):
                log("[!] couldn't return to Lobby — reloading game")
                try:
                    page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(15000)
                    game_frame = find_game_frame(page, max_wait=60)
                    if not game_frame:
                        log("[!] no game frame after reload — stopping")
                        break
                except Exception as e:
                    log(f"[!] reload failed: {e}")
                    break

            # PHASE 1: CLAIM until [claim_min, claim_max]
            log(f"\n=== Phase 1: CLAIM (target balance: [{args.claim_min:,}, {args.claim_max:,}]) ===")
            final_bal, n_ok, n_fail, server_rej = claim_until_threshold(
                game_frame, page, args.claim_min, args.claim_max,
                max_claims=args.max_claims_per_cycle,
                cooldown=args.claim_cooldown,
            )

            if final_bal < args.claim_min and not server_rej:
                log(f"\n[!] couldn't reach {args.claim_min:,} xu — claim exhausted")
                log(f"    current balance: {final_bal:,}")
                # Try surrender anyway if balance >= 40k
                if final_bal >= 40000:
                    log(f"    proceeding to surrender attempt anyway (>=40k)")
                else:
                    log(f"    [STOP] balance too low (<40k)")
                    break

            # PHASE 2: SURRENDER
            log(f"\n=== Phase 2: SURRENDER to target (id={args.target_id}, name={args.target_name!r}) ===")
            ok, lost = surrender_one_round(game_frame, page, args.target_name, args.target_id, args.bet_max,
                                           target_wait_seconds=args.target_wait_seconds,
                                           max_tables_to_scan=args.max_tables_to_scan)
            total_xu_to_target += lost
            cycle_results.append({
                "cycle": cycle_num,
                "balance_after_claim": final_bal,
                "surrender_ok": ok,
                "xu_lost": lost,
            })
            log(f"\n[cycle {cycle_num}] surrender {'✓' if ok else '✗'} | lost {lost:,} xu | total to target: {total_xu_to_target:,}")

            if cycle_num < args.cycles:
                wait = args.delay_between_cycles + random.uniform(0, 3)
                log(f"\n  sleeping {wait:.1f}s before next cycle...")
                time.sleep(wait)
                # Re-find game frame
                new_gf = find_game_frame(page, max_wait=10)
                if new_gf:
                    game_frame = new_gf
                else:
                    log(f"  [reload] game frame gone — reloading")
                    try:
                        page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(15000)
                        game_frame = find_game_frame(page, max_wait=60)
                        if not game_frame:
                            log("  [!] no game frame after reload — stopping")
                            break
                    except Exception as e:
                        log(f"  [!] reload failed: {e}")
                        break

        # Final summary
        bal_final, _ = get_balance(game_frame)
        browser.close()

    log(f"\n{'=' * 60}")
    log(f"FINAL SUMMARY")
    log(f"{'=' * 60}")
    for c in cycle_results:
        marker = "✓" if c['surrender_ok'] else "✗"
        log(f"  Cycle {c['cycle']}: balance_after_claim={c['balance_after_claim']:,}, "
            f"surrender={marker}, lost={c['xu_lost']:,}")
    log(f"\nFinal balance: {bal_final:,} xu")
    log(f"Total xu sent to '{args.target_name}': {total_xu_to_target:,} xu")
    log(f"Total cycles completed: {len(cycle_results)}/{args.cycles}")
    return 0 if cycle_results else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n[EXIT] user interrupt")
        sys.exit(130)
