// 逐幀渲染運鏡影片：對高解析截圖做 Ken Burns 推拉搖移，輸出 1080x1920 MP4
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const ffmpeg = require('ffmpeg-static');
const path = require('path');
const fs = require('fs');

const FPS = 30, W = 1080, H = 1920, OVERLAP = 0.5; // 交叉淡化秒數
// 用法：node render.js <專案名>  → 讀 <專案名>.png / <專案名>-shots.json，輸出 <專案名>.mp4
const NAME = process.argv[2] || 'site-full';

// 分鏡表：center (cx, cy) 與視野寬 vw 皆為原圖像素座標
// 每個鏡頭可用 "img" 指定來源圖（預設 <NAME>.png），支援多屏網站
const shots = require(`./${NAME}-shots.json`);
const pngSize = f => {
  const h = fs.readFileSync(path.resolve(__dirname, f)).subarray(16, 24);
  return { w: h.readUInt32BE(0), h: h.readUInt32BE(4) };
};
for (const s of shots) {
  s.img = s.img || `${NAME}.png`;
  Object.assign(s, { imgW: pngSize(s.img).w, imgH: pngSize(s.img).h });
}

const ease = t => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const lerp = (a, b, t) => a + (b - a) * t;

function camera(shot, localT) {
  const t = ease(Math.min(1, Math.max(0, localT)));
  const cx = lerp(shot.from.cx, shot.to.cx, t);
  const cy = lerp(shot.from.cy, shot.to.cy, t);
  const vw = lerp(shot.from.vw, shot.to.vw, t);
  const s = W / vw;
  let x = W / 2 - cx * s;
  let y = H / 2 - cy * s;
  // 夾住邊界，畫面永遠填滿圖像
  x = Math.min(0, Math.max(W - shot.imgW * s, x));
  y = Math.min(0, Math.max(H - shot.imgH * s, y));
  return { x, y, s, img: shot.img };
}

(async () => {
  const starts = [];
  let t0 = 0;
  for (const s of shots) { starts.push(t0); t0 += s.dur - OVERLAP; }
  const total = t0 + OVERLAP;
  const frames = Math.round(total * FPS);
  console.log(`total ${total.toFixed(1)}s, ${frames} frames`);

  const dir = path.join(__dirname, `frames-${NAME}`);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir);

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: W, height: H } });
  // 用 file:// 開啟，about:blank 來源會被 Chromium 擋掉本機圖片
  const html = path.join(__dirname, 'viewer.html');
  const bg = process.env.BG || '#ece7dc';
  const imgs = [...new Set(shots.map(s => s.img))];
  fs.writeFileSync(html, `<body style="margin:0;overflow:hidden;background:${bg}">
    <div id="A" style="position:absolute"><img src="${imgs[0]}" style="transform-origin:0 0;display:block"></div>
    <div id="B" style="position:absolute;opacity:0"><img src="${imgs[0]}" style="transform-origin:0 0;display:block"></div>
    <div id="fade" style="position:absolute;inset:0;background:${bg};opacity:0"></div>
    ${imgs.map(i => `<img src="${i}" style="display:none">`).join('')}
  </body>`);
  await page.goto('file:///' + html.replace(/\\/g, '/'));
  await page.waitForFunction(() =>
    [...document.images].every(i => i.complete && i.naturalWidth > 0));

  for (let f = 0; f < frames; f++) {
    const t = f / FPS;
    let i = shots.length - 1;
    while (i > 0 && t < starts[i]) i--;
    const a = camera(shots[i], (t - starts[i]) / (shots[i].dur));
    let bOpacity = 0, b = null;
    if (i + 1 < shots.length && t >= starts[i + 1]) {
      bOpacity = (t - starts[i + 1]) / OVERLAP;
      b = camera(shots[i + 1], (t - starts[i + 1]) / shots[i + 1].dur);
    }
    // 頭尾淡入淡出
    const fade = Math.max(0, 1 - t / 0.5, (t - (total - 0.6)) / 0.6);
    await page.evaluate(async ([a, b, bo, fd]) => {
      const set = async (id, c) => {
        const el = document.querySelector(`#${id} img`);
        if (el.getAttribute('src') !== c.img) { el.src = c.img; await el.decode(); }
        el.style.transform = `translate(${c.x}px,${c.y}px) scale(${c.s})`;
      };
      await set('A', a);
      if (b) await set('B', b);
      document.getElementById('B').style.opacity = bo;
      document.getElementById('fade').style.opacity = fd;
    }, [a, b, bOpacity, fade]);
    await page.screenshot({ path: path.join(dir, `f-${String(f).padStart(4, '0')}.jpg`), quality: 92, type: 'jpeg' });
    if (f % 60 === 0) console.log(`frame ${f}/${frames}`);
  }
  await browser.close();

  execFileSync(ffmpeg, ['-y', '-framerate', String(FPS), '-i', path.join(dir, 'f-%04d.jpg'),
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', path.join(__dirname, `${NAME}.mp4`)],
    { stdio: 'ignore' });
  console.log(`done → ${NAME}.mp4`);
})();
