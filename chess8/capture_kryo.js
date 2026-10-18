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
    args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage','--window-size=1280,720'],
    defaultViewport:{width:1280,height:720},
  });
  const page = await browser.newPage();
  await browser.setCookie(
    { name: '__Host-AccessToken', value: ACCESS, domain: 'game8.io', path: '/', httpOnly: true, secure: true, sameSite: 'None' },
    { name: '__Host-RefreshToken', value: REFRESH, domain: 'game8.io', path: '/', httpOnly: true, secure: true, sameSite: 'None' },
    { name: 'g_state', value: GSTATE, domain: 'game8.io', path: '/' },
  );
  const cdp = await page.createCDPSession();
  await cdp.send('Fetch.enable', { patterns: [
    { urlPattern: '*player/sign*', requestStage: 'Response' },
    { urlPattern: '*chess.game8.io/kryo*', requestStage: 'Request' },
    { urlPattern: '*chess.game8.io/kryo*', requestStage: 'Response' },
  ]});
  cdp.on('Fetch.requestPaused', async (event) => {
    try {
      const url = event.request.url;
      if (event.responseStatusCode) {
        let body;
        const r = await cdp.send('Fetch.getResponseBody', {requestId: event.requestId});
        body = r.base64Encoded ? Buffer.from(r.body, 'base64') : Buffer.from(r.body, 'latin1');
        if (/player\/sign/.test(url)) {
          fs.writeFileSync('path.join(OUT, 'sign_resp.json')', body.toString('utf8'));
          console.log('sign', body.toString('utf8').slice(0,80));
        }
        if (/kryo/.test(url)) {
          fs.writeFileSync('path.join(OUT, 'kryo_res.bin')', body);
          console.log('kryo res', body.length);
        }
        await cdp.send('Fetch.continueResponse', {requestId: event.requestId}).catch(async()=>{
          await cdp.send('Fetch.continueRequest', {requestId: event.requestId});
        });
      } else {
        await cdp.send('Fetch.continueRequest', {requestId: event.requestId});
      }
    } catch(e) {
      try { await cdp.send('Fetch.continueRequest', {requestId: event.requestId}); } catch {}
    }
  });
  await page.goto('https://game8.io/game/chess-club', { waitUntil:'domcontentloaded', timeout:120000 });
  await page.evaluate(() => {
    const el=[...document.querySelectorAll('a,button,div')].find(e => (e.textContent||'').trim()==='Play now');
    if (el) el.click();
  });
  for (let i=0;i<50;i++) {
    if (fs.existsSync('path.join(OUT, 'kryo_res.bin')') && fs.existsSync('path.join(OUT, 'sign_resp.json')')) break;
    await new Promise(r=>setTimeout(r,400));
  }
  await browser.close();
  console.log('CAPTURE_DONE');
})().catch(e=>{console.error(e); process.exit(1);});
