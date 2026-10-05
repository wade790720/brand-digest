// 截取網站高解析全頁截圖
const { chromium } = require('playwright');

(async () => {
  const url = process.argv[2] || 'http://tekuei.com/';
  const out = process.argv[3] || 'site-full.png';
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 3, // 3x 解析度，特寫不糊
  });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
  // 慢速捲到底觸發 lazy-load，再捲回頂
  await page.evaluate(async () => {
    for (let y = 0; y < document.body.scrollHeight; y += 600) {
      window.scrollTo(0, y);
      await new Promise(r => setTimeout(r, 150));
    }
    window.scrollTo(0, 0);
  });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: out, fullPage: true });
  const h = await page.evaluate(() => document.body.scrollHeight);
  console.log(`done. page height: ${h}px css → image: ${1440 * 3}x${h * 3}px`);
  await browser.close();
})();
