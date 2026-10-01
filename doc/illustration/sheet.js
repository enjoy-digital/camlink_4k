// usage: node sheet.js <url> <prefix> W H t1 t2 ...
const puppeteer = require('puppeteer-core');
(async () => {
  const [url, prefix, W, H, ...ts] = process.argv.slice(2);
  const browser = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', headless: 'new',
    args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--hide-scrollbars'] });
  const page = await browser.newPage();
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  page.on('console', m => { if (m.type() === 'error' || m.type() === 'warn') console.log('[page]', m.text()); });
  await page.setViewport({ width: +W, height: +H });
  await page.goto(url, { waitUntil: 'networkidle0' });
  await page.waitForFunction('window.sceneReady === true', { timeout: 120000 });
  for (const t of ts) {
    await page.evaluate(t => window.renderAt(t), +t);
    await page.screenshot({ path: `${prefix}_${String(t).padStart(5, '0')}.png` });
  }
  await browser.close();
})();
