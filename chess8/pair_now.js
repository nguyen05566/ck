const puppeteer = require('/tmp/pw/node_modules/puppeteer-core');
const fs = require('fs');
const path = require('path');
const OUT = __dirname;
const ACCESS = process.env.ACCESS;
const REFRESH = process.env.REFRESH;
const GSTATE = process.env.GSTATE;

(async()=>{
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME_PATH || '/data/data/com.termux/files/usr/bin/chromium-browser',
    headless: 'new',
    args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
      '--use-gl=angle','--use-angle=swiftshader-webgl','--enable-unsafe-swiftshader',
      '--enable-webgl','--ignore-gpu-blocklist','--window-size=1280,720'],
    defaultViewport:{width:1280,height:720},
  });
  const page = await browser.newPage();

  await page.evaluateOnNewDocument(() => {
    const OrigWS = window.WebSocket;
    if (!OrigWS) return;
    window.__wsHooked = true;
    window.__wsSends = [];
    window.__wsRecvs = [];
    function Wrapped(url, protocols) {
      window.__wsUrl = String(url);
      const sock = protocols === undefined ? new OrigWS(url) : new OrigWS(url, protocols);
      sock.addEventListener('message', (e) => {
        if (e.data instanceof ArrayBuffer) window.__wsRecvs.push(Array.from(new Uint8Array(e.data)));
      });
      const origSend = sock.send.bind(sock);
      sock.send = function(data) {
        if (data instanceof ArrayBuffer) window.__wsSends.push(Array.from(new Uint8Array(data)));
        return origSend(data);
      };
      return sock;
    }
    Wrapped.prototype = OrigWS.prototype;
    window.WebSocket = Wrapped;
  });

  await browser.setCookie(
    { name: '__Host-AccessToken', value: ACCESS, domain: 'game8.io', path: '/', httpOnly: true, secure: true, sameSite: 'None' },
    { name: '__Host-RefreshToken', value: REFRESH, domain: 'game8.io', path: '/', httpOnly: true, secure: true, sameSite: 'None' },
    { name: 'g_state', value: GSTATE, domain: 'game8.io', path: '/' },
  );

  const cdp = await page.createCDPSession();
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*chess.game8.io/kryo*', requestStage: 'Response' }] });
  cdp.on('Fetch.requestPaused', async (event) => {
    try {
      const r = await cdp.send('Fetch.getResponseBody', {requestId: event.requestId});
      let body = r.base64Encoded ? Buffer.from(r.body, 'base64') : Buffer.from(r.body, 'latin1');
      // CDP may mangle binary; try latin1 re-encode issues - also try base64 of raw
      if (body.length > 100) {
        fs.writeFileSync('path.join(OUT, 'kryo_res.bin')', body);
        console.log('kryo', body.length, body.slice(0,8).toString('hex'));
      }
      await cdp.send('Fetch.continueResponse', {requestId: event.requestId}).catch(async()=>{
        await cdp.send('Fetch.continueRequest', {requestId: event.requestId});
      });
    } catch(e) {
      try { await cdp.send('Fetch.continueRequest', {requestId: event.requestId}); } catch {}
    }
  });

  await page.goto('https://game8.io/game/chess-club', { waitUntil:'networkidle2', timeout:120000 }).catch(()=>{});

  // Click Play now - try multiple times
  for (let t=0;t<5;t++) {
    const clicked = await page.evaluate(() => {
      const els=[...document.querySelectorAll('a,button,div,span')];
      const el=els.find(e => (e.textContent||'').trim()==='Play now');
      if (el) { el.click(); return true; }
      return false;
    });
    console.log('play click', clicked);
    for (let i=0;i<15;i++) {
      const gf = page.frames().find(f => /revision\.game8/.test(f.url()));
      if (gf) { console.log('frame ok'); break; }
      await new Promise(r=>setTimeout(r,1000));
    }
    if (page.frames().some(f => /revision\.game8/.test(f.url()))) break;
  }

  const gameFrame = page.frames().find(f => /revision\.game8/.test(f.url()));
  if (!gameFrame) {
    console.log('frames', page.frames().map(f=>f.url().slice(0,80)));
    await browser.close(); process.exit(1);
  }

  let dump = {sends:[], recvs:[], hooked:false};
  for (let i=0;i<60;i++) {
    dump = await gameFrame.evaluate(() => ({
      hooked: !!window.__wsHooked,
      url: window.__wsUrl || null,
      sends: window.__wsSends || [],
      recvs: window.__wsRecvs || [],
    })).catch(() => dump);
    if (dump.sends.length >= 1) break;
    await new Promise(r=>setTimeout(r,500));
  }
  await new Promise(r=>setTimeout(r,1500));
  dump = await gameFrame.evaluate(() => ({
    hooked: !!window.__wsHooked,
    url: window.__wsUrl || null,
    sends: window.__wsSends || [],
    recvs: window.__wsRecvs || [],
  })).catch(() => dump);

  console.log('hooked', dump.hooked, 'ws', dump.url, 'sends', dump.sends.length, 'recvs', dump.recvs.length);
  // wait more + click game area to trigger login/WS
  const box = await page.evaluate(() => {
    const f=[...document.querySelectorAll('iframe')].find(x=>/revision\.game8/.test(x.src)||x.title==='Chess club');
    if (!f) return null;
    const r=f.getBoundingClientRect();
    return {x:r.x,y:r.y,w:r.width,h:r.height};
  });
  if (box) {
    for (let k=0;k<8;k++) {
      await page.mouse.click(box.x+box.w*0.5, box.y+box.h*(0.3+0.08*k));
      await new Promise(r=>setTimeout(r,1500));
      dump = await gameFrame.evaluate(() => ({
        hooked: !!window.__wsHooked,
        url: window.__wsUrl || null,
        sends: window.__wsSends || [],
        recvs: window.__wsRecvs || [],
      })).catch(() => dump);
      console.log('after click', k, 'sends', dump.sends.length, 'ws', dump.url);
      if (dump.sends.length >= 1) break;
    }
  }
  const kryo = fs.existsSync('path.join(OUT, 'kryo_res.bin')') ? Array.from(fs.readFileSync('path.join(OUT, 'kryo_res.bin')')) : null;
  fs.writeFileSync('path.join(OUT, 'pair.json')', JSON.stringify({kryo, sends: dump.sends, recvs: dump.recvs.slice(0,15), hooked: dump.hooked}));
  if (dump.sends[0]) console.log('LOGIN', Buffer.from(dump.sends[0]).toString('hex'));
  await browser.close();
})().catch(e=>{console.error(e); process.exit(1);});
