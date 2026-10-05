// 互動實錄共用：啟動錄影、游標注入（每頁自動）、緩動捲動、滑鼠慢移
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

async function start(url, out) {
  const dir = path.join(__dirname, 'live-rec-' + out);
  fs.rmSync(dir, { recursive: true, force: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 810, height: 1440 },
    recordVideo: { dir, size: { width: 810, height: 1440 } },
  });
  // 每個頁面載入時自動注入游標（跨頁導航也不會消失）
  await ctx.addInitScript(() => addEventListener('DOMContentLoaded', () => {
    const c = document.createElement('div');
    c.style.cssText = `position:fixed;z-index:2147483647;width:22px;height:22px;border-radius:50%;
      background:rgba(40,35,30,.75);border:2px solid rgba(255,255,255,.9);pointer-events:none;
      transform:translate(-50%,-50%);transition:width .15s,height .15s;left:-50px;top:-50px;
      box-shadow:0 1px 6px rgba(0,0,0,.3)`;
    document.body.appendChild(c);
    addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
    addEventListener('mousedown', () => { c.style.width = '16px'; c.style.height = '16px'; }, true);
    addEventListener('mouseup', () => { c.style.width = '22px'; c.style.height = '22px'; }, true);
  }));
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(2500);

  const glide = (y, ms) => page.evaluate(([y, ms]) => new Promise(res => {
    const y0 = scrollY, t0 = performance.now();
    const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    (function step(t) {
      const p = Math.min(1, (t - t0) / ms);
      scrollTo(0, y0 + (y - y0) * ease(p));
      p < 1 ? requestAnimationFrame(step) : res();
    })(t0);
  }), [y, ms]);

  const drift = (x, y, steps = 40) => page.mouse.move(x, y, { steps });

  const hover = async (locator, dwell = 1000) => {
    const b = await locator.boundingBox().catch(() => null);
    if (b) { await drift(b.x + b.width / 2, b.y + b.height / 2, 40); await page.waitForTimeout(dwell); }
    return !!b;
  };

  const finish = async () => {
    await ctx.close();
    await browser.close();
    const webm = fs.readdirSync(dir).find(f => f.endsWith('.webm'));
    fs.copyFileSync(path.join(dir, webm), path.join(__dirname, `${out}.webm`));
    console.log(`done → ${out}.webm`);
  };

  return { page, glide, drift, hover, finish };
}

module.exports = { start };
