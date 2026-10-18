#!/usr/bin/env python3
"""Game8.io Chess Club — WebSocket client

Env: ACCESS, REFRESH, GSTATE, optional CHROME_PATH
Uses cookies via browser Kryo login, then WS LOGIN_KRYO + lobby + moves.
"""
import json, os, struct, ssl, socket, base64, time, subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parent
ACCESS = os.environ['ACCESS']
REFRESH = os.environ['REFRESH']
GSTATE = os.environ.get('GSTATE', '{"i_l":0}')

def ig(s):
    b = s.encode(); return bytes([len(b)]) + b
def aak(v):
    return struct.pack('>Q', v & ((1<<64)-1))
def cid(n):
    return struct.pack('>H', n)
def write_varint(v):
    v &= (1<<64)-1; o=bytearray()
    while True:
        if (v & ~0x7F)==0: o.append(v); break
        o.append((v&0x7F)|0x80); v>>=7
    return bytes(o)
def read_varint(data, i):
    value=0; shift=0
    while True:
        b=data[i]; i+=1
        value |= (b&0x7f)<<shift
        if (b&0x80)==0: break
        shift += 7
    return value,i

def capture():
    for f in ['kryo_res.bin']:
        try: (OUT/f).unlink()
        except FileNotFoundError: pass
    env = os.environ.copy()
    env.update(ACCESS=ACCESS, REFRESH=REFRESH, GSTATE=GSTATE)
    r = subprocess.run(['node', str(OUT/'pair_now.js')], env=env,
                       capture_output=True, text=True, timeout=180)
    pair = json.loads((OUT/'pair.json').read_text())
    if not pair.get('sends'):
        raise RuntimeError('no LOGIN_KRYO captured; '+ (r.stdout or '')[-300:])
    login = bytes(pair['sends'][0])
    followups = [bytes(s) for s in pair['sends'][1:]]
    return login, followups, pair

class WS:
    def __init__(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /ws/gameServer HTTP/1.1\r\nHost: chess.game8.io\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               "Origin: https://revision.game8.io\r\n\r\n")
        ctx = ssl.create_default_context()
        sock = socket.create_connection(('chess.game8.io', 443), timeout=20)
        self.ss = ctx.wrap_socket(sock, server_hostname='chess.game8.io')
        self.ss.sendall(req.encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            c = self.ss.recv(4096)
            if not c: break
            buf += c
        assert b'101' in buf.split(b'\r\n',1)[0]
        self.buf = buf.split(b'\r\n\r\n',1)[1]
        self.ss.settimeout(4.0)
    def send(self, payload: bytes):
        mask = os.urandom(4)
        hdr = bytearray([0x80|2]); n = len(payload)
        if n < 126: hdr.append(0x80|n)
        elif n < 65536:
            hdr.append(0x80|126); hdr += struct.pack('!H', n)
        else:
            hdr.append(0x80|127); hdr += struct.pack('!Q', n)
        hdr += mask
        self.ss.sendall(hdr + bytes(b^mask[i%4] for i,b in enumerate(payload)))
    def recv(self, timeout=4.0, max_frames=80):
        self.ss.settimeout(timeout)
        buf = self.buf; self.buf = b''; frames = []
        end = time.time() + timeout
        while time.time() < end and len(frames) < max_frames:
            try:
                data = self.ss.recv(65536)
                if not data: break
                buf += data
            except socket.timeout:
                if frames: break
                continue
            while True:
                if len(buf) < 2: break
                b0,b1 = buf[0], buf[1]
                op = b0 & 0x0f; masked = b1 & 0x80; n = b1 & 0x7f; off = 2
                if n == 126:
                    if len(buf) < off+2: break
                    n = struct.unpack('!H', buf[off:off+2])[0]; off += 2
                elif n == 127:
                    if len(buf) < off+8: break
                    n = struct.unpack('!Q', buf[off:off+8])[0]; off += 8
                if masked:
                    if len(buf) < off+4: break
                    mask = buf[off:off+4]; off += 4
                if len(buf) < off+n: break
                p = buf[off:off+n]
                if masked: p = bytes(p[i]^mask[i%4] for i in range(n))
                frames.append((op, p)); buf = buf[off+n:]
                if op == 8:
                    self.buf = b''; return frames
        self.buf = buf
        return frames
    def close(self):
        try: self.ss.close()
        except: pass

def decode(p):
    if len(p) < 2: return None, p.hex()
    class_id = struct.unpack('>H', p[:2])[0]
    body = p[2:]
    names = {
        300:'PONG', 301:'PING', 302:'LOGIN', 401:'ENTER_PLACE', 406:'PLAYER_ENTERED?',
        407:'PLAYER_EXITED', 408:'QUICK_PLAY', 417:'START_MATCH', 418:'GAMEOVER',
        420:'SET_TURN', 421:'SET_PLAYER_STATUS', 431:'BALANCE_CHANGED',
        433:'GET_TABLE_DATA_EX', 434:'SET_READY', 436:'SCORE_CHANGED', 529:'MOVE',
        601:'LOGIN_EX',
    }
    name = names.get(class_id, '')
    if body[:1] == b'\xff':
        try:
            s = body[1:].decode('utf-16-be', 'replace')
            if any(c.isalpha() for c in s[:40]):
                return class_id, f'{name} MSG {s[:160]}'
        except: pass
    asc = ''.join(chr(c) if 32<=c<127 else '.' for c in body[:50])
    return class_id, f'{name} len={len(body)} {body[:24].hex()} | {asc}'

def main():
    print('=== Capture fresh session (browser Kryo + WS hook) ===')
    login, followups, pair = capture()
    print('LOGIN_KRYO', login.hex())
    print('followups', len(followups), [f.hex()[:40] for f in followups])

    ws = WS()
    print('WS connected wss://chess.game8.io/ws/gameServer')

    print('\n=== LOGIN_KRYO ===')
    ws.send(login)
    for op, p in ws.recv(5.0):
        if op == 8:
            print('CLOSE', p.hex()); continue
        print(' ', decode(p))

    print('\n=== Lobby (replay game handshake) ===')
    for f in followups:
        ws.send(f)
        for op, p in ws.recv(2.5):
            print(' ', decode(p))

    print('\n=== PING ===')
    ws.send(cid(301))
    for op, p in ws.recv(1.5):
        print(' ', decode(p))

    print('\n=== Read match traffic / wait ===')
    moves = []
    for i in range(15):
        frames = ws.recv(2.0)
        if not frames:
            ws.send(cid(301))
            continue
        for op, p in frames:
            c, info = decode(p)
            print(f'  class={c} {info}')
            if c == 529 and len(p) >= 4:
                moves.append(p[2:5])
            if c == 418:
                print('  *** GAME OVER ***')

    if moves:
        print(f'\nReceived {len(moves)} MOVE frames. Last:', moves[-1].hex())
        # Send a MOVE in same 3-byte format seen from server (from,to,0xff)
        # Only send if last move suggests it might be our turn — otherwise just show capability
        last = moves[-1]
        # echo-style reply often uses same layout
        reply = cid(529) + last[:2] + b'\xff'
        print('Example MOVE payload would be:', reply.hex(), '(not auto-sent to avoid bad moves)')

    ws.close()
    print('\nDone. Session is live on chess.game8.io WebSocket.')

if __name__ == '__main__':
    main()
