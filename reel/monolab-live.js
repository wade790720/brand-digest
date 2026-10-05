// monolab 互動實錄：首頁 slide 轉場 → Art 內頁瀏覽
const { start } = require('./live-lib');

(async () => {
  const { page, glide, drift, hover, finish } = await start('https://monolab.world/en/', 'monolab-live');

  // 1. 首頁：滾輪推進兩個 slide，錄轉場動畫
  await drift(405, 900, 30);
  await page.waitForTimeout(600);
  await page.mouse.wheel(0, 900);
  await page.waitForTimeout(2600);
  await page.mouse.wheel(0, 900);
  await page.waitForTimeout(2600);

  // 2. 進 Art 內頁（nav 直接點；被漢堡選單藏住就先開選單；再不行直接 goto）
  try {
    await page.click('text=Art', { timeout: 3000 });
  } catch {
    try {
      await page.click('button:has-text("menu"), .hamburger, [class*=burger], [class*=menu-btn]', { timeout: 2000 });
      await page.waitForTimeout(800);
      await page.click('text=Art', { timeout: 3000 });
    } catch {
      await page.goto('https://monolab.world/en/art/', { waitUntil: 'load' });
    }
  }
  await page.waitForTimeout(3000);

  // 3. 內頁：緩捲瀏覽、hover 作品、點進一件作品
  const h = await page.evaluate(() => document.body.scrollHeight);
  await glide(h * 0.22, 2600);
  await page.waitForTimeout(500);
  const item = page.locator('main a:has(img), article a:has(img), a:has(img)').nth(1);
  await hover(item, 1100);
  try {
    await Promise.all([page.waitForLoadState('load'), item.click({ timeout: 3000 })]);
    await page.waitForTimeout(2500);
    const h2 = await page.evaluate(() => document.body.scrollHeight);
    await glide(h2 * 0.35, 2800);
    await page.waitForTimeout(800);
  } catch {
    await glide(h * 0.55, 2800);
    await page.waitForTimeout(800);
  }

  await finish();
})();
