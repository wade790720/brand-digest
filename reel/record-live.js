// 互動實錄：9:16 視窗實際操作網頁並錄影（捲動、hover、點擊），注入可見游標
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

(async () => {
  const url = process.argv[2] || 'http://tekuei.com/';
  const out = process.argv[3] || 'live';
  const dir = path.join(__dirname, 'live-rec');
  fs.rmSync(dir, { recursive: true, force: true });

  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 810, height: 1440 },
    recordVideo: { dir, size: { width: 810, height: 1440 } },
  });
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(2500);

  // 注入游標圓點，跟著滑鼠走
  await page.evaluate(() => {
    const c = document.createElement('div');
    c.id = '__cur';
    c.style.cssText = `position:fixed;z-index:99999;width:22px;height:22px;border-radius:50%;
      background:rgba(40,35,30,.75);border:2px solid rgba(255,255,255,.9);pointer-events:none;
      transform:translate(-50%,-50%);transition:width .15s,height .15s;left:-50px;top:-50px;
      box-shadow:0 1px 6px rgba(0,0,0,.3)`;
    document.body.appendChild(c);
    window.addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
    window.addEventListener('mousedown', () => { c.style.width = '16px'; c.style.height = '16px'; }, true);
    window.addEventListener('mouseup', () => { c.style.width = '22px'; c.style.height = '22px'; }, true);
  });

  // rAF 緩動捲動
  const glide = (y, ms) => page.evaluate(([y, ms]) => new Promise(res => {
    const y0 = scrollY, t0 = performance.now();
    const ease = t => t < .5 ? 4*t*t*t : 1 - Math.pow(-2*t+2, 3)/2;
    (function step(t) {
      const p = Math.min(1, (t - t0) / ms);
      scrollTo(0, y0 + (y - y0) * ease(p));
      p < 1 ? requestAnimationFrame(step) : res();
    })(t0);
  }), [y, ms]);

  // 滑鼠慢移（分段插值，游標平滑）
  const drift = async (x, y, steps = 40) => { await page.mouse.move(x, y, { steps }); };

  await page.waitForTimeout(800);
  // 1. hero：游標滑向 BEGIN 按鈕 hover
  await drift(300, 700, 30);
  await page.waitForTimeout(300);
  const begin = page.locator('a,button').filter({ hasText: /begin/i }).first();
  const bb = await begin.boundingBox().catch(() => null);
  if (bb) { await drift(bb.x + bb.width / 2, bb.y + bb.height / 2, 45); await page.waitForTimeout(1200); }

  // 2. 平滑捲到作品區，游標跟著瀏覽
  const h = await page.evaluate(() => document.body.scrollHeight);
  await glide(h * 0.36, 2600);
  await page.waitForTimeout(400);
  // hover 作品卡片
  const card = page.locator('text=MONOLAB').first();
  const cb = await card.boundingBox().catch(() => null);
  if (cb) { await drift(cb.x + cb.width / 2, cb.y - 60, 40); await page.waitForTimeout(1200); }
  else { await drift(400, 800, 40); await page.waitForTimeout(800); }

  // 3. 續捲到 CTA，hover 按鈕並點擊
  await glide(h * 0.86, 2800);
  await page.waitForTimeout(400);
  const cta = page.locator('a,button').filter({ hasText: /journey|begin/i }).last();
  const tb = await cta.boundingBox().catch(() => null);
  if (tb) {
    await drift(tb.x + tb.width / 2, tb.y + tb.height / 2, 45);
    await page.waitForTimeout(900);
    await page.mouse.down(); await page.waitForTimeout(120); await page.mouse.up();
    await page.waitForTimeout(1800); // 讓跳轉/動畫入鏡
  }

  await ctx.close(); // flush 影片
  await browser.close();
  const webm = fs.readdirSync(dir).find(f => f.endsWith('.webm'));
  fs.copyFileSync(path.join(dir, webm), path.join(__dirname, `${out}.webm`));
  console.log(`done → ${out}.webm`);
})();
