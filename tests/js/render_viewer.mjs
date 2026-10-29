// Renders slideshow states in headless Chromium. Usage: node render_viewer.mjs '<json>'
// args: {file, outDir, width?, height?, shots: [{hash: "layout=6&color=0&paused=1&at=0&hide=1"}]}
// Writes shot_<i>.png (the canvas) and prints {n, errors, info:[...]} as JSON.
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
let playwright;
try { playwright = require("playwright"); } catch { playwright = require("/opt/node-tools/node_modules/playwright"); }

const args = JSON.parse(process.argv[2]);
const browser = await playwright.chromium.launch();
const context = await browser.newContext({ viewport: { width: args.width ?? 1920, height: args.height ?? 1080 }, deviceScaleFactor: 1 });
const errors = [], info = [];
for (const [i, shot] of args.shots.entries()) {
  const page = await context.newPage();
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  await page.goto(pathToFileURL(args.file).href + "#" + shot.hash);
  await page.waitForFunction(() => window.qrViewer);
  info.push(await page.evaluate(() => ({ P: window.qrViewer.perPage(), N: window.qrViewer.N, K: window.qrViewer.K, at: window.qrViewer.st.cursor, text: document.getElementById("info").textContent })));
  await page.locator("#c").screenshot({ path: `${args.outDir}/shot_${i}.png` });
  await page.close();
}
console.log(JSON.stringify({ n: args.shots.length, errors, info }));
await browser.close();
