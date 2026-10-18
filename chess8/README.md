# chess8 — Game8.io Chess Club WebSocket

Phần đã hoàn thành:
- Reverse protocol WS `wss://chess.game8.io/ws/gameServer`
- Login `LOGIN_KRYO` bằng sessionId + secret (lấy từ HTTP Kryo login)
- Vào lobby, nhận `START_MATCH` / `SET_TURN` / `MOVE`

Phần sẽ phát triển sau:
- Auto-move khi tới lượt (encode square từ algebraic)
- Board state parser đầy đủ
- Matchmaking / QUICK_PLAY tự động

## Yêu cầu

- Node.js + puppeteer-core
- Chromium/Chrome headless (`chromium-browser` trên Termux)
- Cookies Game8: `__Host-AccessToken`, `__Host-RefreshToken`

## Chạy

```bash
# từ thư mục chess8/
npm install puppeteer-core   # hoặc dùng path có sẵn

export ACCESS='__Host-AccessToken JWT...'
export REFRESH='__Host-RefreshToken JWT...'
export GSTATE='{"i_l":0}'

# cần binary chromium path trong pair_now.js (executablePath)
node pair_now.js             # capture session (browser)
python3 play.py              # login WS + đọc traffic trận
```

`play.py` sẽ:
1. Chạy `pair_now.js` để lấy LOGIN_KRYO + follow-up từ game thật
2. Mở WebSocket, gửi LOGIN_KRYO
3. Replay ENTER_PLACE / GET_TABLE
4. Đọc PING/PONG, START_MATCH, MOVE

## Protocol (đã reverse)

| Class ID | Message |
|---------|---------|
| 300 | PONG |
| 301 | PING |
| 401 | ENTER_PLACE |
| 406 | player profile / entered |
| 408 | QUICK_PLAY |
| 417 | START_MATCH |
| 420 | SET_TURN |
| 529 | MOVE (`from,to,0xFF`) |
| 601 | LOGIN_EX (error channel) |

LOGIN_KRYO wire:

```
0xF6 + "LOGIN_KRYO"
Ig("html") + Ig("5.0.3")
u64be(sessionId) + u64be(secret)
Ig("") + Ig("en") + Ig("chess")
```

## Cookies

**Không commit cookie/token vào repo.** Dùng env var.
