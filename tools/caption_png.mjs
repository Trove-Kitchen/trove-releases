// One preview caption as a transparent PNG (886 px wide): white Inter Display text on a
// Trove orange pill, centred. store_shots.sh copies this next to trove-app's screenshot
// builder (where Playwright is installed) and overlays the PNG on the clip with ffmpeg.
//   node caption_png.mjs <text file> <fonts dir> <out.png>
import { chromium } from 'playwright';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
const [textFile, fonts, out] = process.argv.slice(2);
const text = readFileSync(textFile, 'utf8').trim();
const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const font = `file://${resolve(fonts, 'InterDisplay-Black.otf')}`;
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 886, height: 400 }, deviceScaleFactor: 1 });
await page.setContent(`<!doctype html><html><head><style>
  @font-face{font-family:"Inter Display";font-weight:900;src:url("${font}")}
  html,body{margin:0;background:transparent}
  .wrap{width:886px;display:flex;justify-content:center;padding:12px 0}
  .pill{max-width:760px;background:#E9502F;color:#fff;font:900 46px/1.12 "Inter Display",sans-serif;
        letter-spacing:-0.5px;text-align:center;padding:22px 34px;border-radius:30px;text-wrap:balance;
        box-shadow:0 10px 30px rgba(60,15,5,.35)}
</style></head><body><div class="wrap"><div class="pill">${esc(text)}</div></div></body></html>`);
await page.evaluate(() => document.fonts.ready);
const box = await page.locator('.wrap').boundingBox();
await page.screenshot({ path: out, omitBackground: true, clip: { x: 0, y: 0, width: 886, height: Math.ceil(box.height) } });
await browser.close();
