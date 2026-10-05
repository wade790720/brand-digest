// 針對 scroll-jacking 單屏網站：滾輪逐屏推進，每屏截一張 3x 視窗圖
const { chromium } = require('playwright');

(async () => {
  const url = process.argv[2];
  const name = process.argv[3] || 'slides';
  const count = Number(process.argv[4] || 6);
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 3,
  });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(2000);
  for (let i = 0; i < count; i++) {
    await page.screenshot({ path: `${name}-s${i}.png` });
    console.log(`slide ${i} saved`);
    await page.mouse.wheel(0, 900);
    await page.waitForTimeout(2200); // 等轉場動畫
  }
  await browser.close();
})();
