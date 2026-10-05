// matsu 互動實錄：首頁緩捲 → /artworks/ 作品列表 → 藝術家內頁
const { start } = require('./live-lib');

(async () => {
  const { page, glide, drift, hover, finish } = await start('https://matsubiennial.tw/', 'matsu-live');

  // 1. 首頁：緩捲到藝術作品輪播區，hover 卡片
  await drift(405, 900, 30);
  const h = await page.evaluate(() => document.body.scrollHeight);
  await glide(h * 0.30, 3000);
  await page.waitForTimeout(500);
  await hover(page.locator('a:has(img)').nth(2), 1500);

  // 2. 作品列表頁：緩捲兩段、hover 作品卡
  await page.goto('https://matsubiennial.tw/artworks/#all', { waitUntil: 'load' });
  await page.waitForTimeout(2800);
  const h2 = await page.evaluate(() => document.body.scrollHeight);
  await glide(h2 * 0.22, 2600);
  await page.waitForTimeout(400);
  await hover(page.locator('a:has(img)').nth(4), 1200);
  await glide(h2 * 0.42, 2600);
  await page.waitForTimeout(600);

  // 3. 藝術家內頁：載入後緩捲瀏覽
  await page.goto('https://matsubiennial.tw/artists/details/321', { waitUntil: 'load' });
  await page.waitForTimeout(2500);
  const h3 = await page.evaluate(() => document.body.scrollHeight);
  await drift(405, 800, 35);
  await glide(h3 * 0.35, 3000);
  await page.waitForTimeout(900);

  await finish();
})();
