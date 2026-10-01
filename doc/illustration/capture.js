// Deterministic frame capture: page exposes window.renderAt(t) and window.DURATION.
// usage: node capture.js <url> <out.mp4> [W] [H] [fps] [t0] [t1]
const puppeteer = require('puppeteer-core');
const { spawn } = require('child_process');

(async () => {
  const [url, out, W = 1920, H = 1080, FPS = 30, T0, T1] = process.argv.slice(2);
  const w = +W, h = +H, fps = +FPS;
  const browser = await puppeteer.launch({
    executablePath: '/usr/bin/google-chrome', headless: 'new',
    args: ['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--hide-scrollbars'],
  });
  const page = await browser.newPage();
  page.on('console', m => console.log('[page]', m.text()));
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  await page.setViewport({ width: w, height: h, deviceScaleFactor: 1 });
  await page.goto(url, { waitUntil: 'networkidle0' });
  await page.waitForFunction('window.sceneReady === true', { timeout: 120000 });
  const dur = await page.evaluate('window.DURATION');
  const t0 = T0 !== undefined ? +T0 : 0, t1 = T1 !== undefined ? +T1 : dur;
  const n = Math.round((t1 - t0) * fps);

  const still = out.endsWith('.png');
  const ff = still ? null : spawn('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error',
    '-f', 'image2pipe', '-framerate', String(fps), '-i', '-',
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '16', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out],
    { stdio: ['pipe', 'inherit', 'inherit'] });

  const start = Date.now();
  for (let i = 0; i < (still ? 1 : n); i++) {
    const t = t0 + i / fps;
    await page.evaluate(t => window.renderAt(t), t);
    const buf = await page.screenshot({ type: 'png', clip: { x: 0, y: 0, width: w, height: h } });
    if (still) { require('fs').writeFileSync(out, buf); break; }
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    if (i % 30 === 0) console.log(`frame ${i}/${n}  t=${t.toFixed(2)}  ${((Date.now() - start) / 1000).toFixed(0)}s`);
  }
  if (ff) { ff.stdin.end(); await new Promise(r => ff.on('close', r)); }
  await browser.close();
  console.log('done', out);
})();
