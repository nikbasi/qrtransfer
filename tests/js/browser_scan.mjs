// Drives web/ in headless Chromium. Usage: node browser_scan.mjs '<json args>'
// args: {url, video?: mjpeg path (fake camera), photos?: [paths], password?, timeoutMs?}
// Prints {name, sha256, meta, status, errors} as JSON.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let playwright;
try {
  playwright = require("playwright");
} catch {
  playwright = require("/opt/node-tools/node_modules/playwright");
}

const args = JSON.parse(process.argv[2]);
const launchArgs = ["--use-fake-ui-for-media-stream"];
if (args.video) launchArgs.push("--use-fake-device-for-media-stream", `--use-file-for-fake-video-capture=${args.video}`);
const browser = await playwright.chromium.launch({ args: launchArgs });
const context = await browser.newContext({ acceptDownloads: true, permissions: ["camera"] });
const page = await context.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));

const out = { errors };
try {
  await page.goto(args.url);
  if (args.video) await page.click("#start");
  else await page.setInputFiles("#photos", args.photos);

  const deadline = Date.now() + (args.timeoutMs ?? 120000);
  let passwordTries = 0;
  for (;;) {
    if (Date.now() > deadline) throw new Error("timeout; status: " + (await page.textContent("#status")));
    if (await page.evaluate(() => document.body.dataset.state === "done")) break;
    if (await page.isVisible("#pw-dialog[open]")) {
      if (passwordTries++ > 0 && !args.allowRetry) throw new Error("password rejected");
      await page.fill("#pw-input", args.password ?? "");
      await page.click("#pw-ok");
    }
    await page.waitForTimeout(200);
  }
  out.name = await page.textContent("#r-name");
  out.meta = await page.textContent("#r-meta");
  const [download] = await Promise.all([page.waitForEvent("download"), page.click("#r-save")]);
  out.sha256 = createHash("sha256").update(readFileSync(await download.path())).digest("hex");
} catch (e) {
  out.error = String(e);
}
out.status = await page.textContent("#status").catch(() => null);
console.log(JSON.stringify(out));
await browser.close();
