// Opens the in-memory slideshow in headless Chromium. Usage: node check_url.mjs <url>
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
let playwright;
try { playwright = require("playwright"); } catch { playwright = require("/opt/node-tools/node_modules/playwright"); }
const b = await playwright.chromium.launch();
const p = await (await b.newContext({ viewport: { width: 1280, height: 800 } })).newPage();
const errors = [];
p.on("pageerror", (e) => errors.push(String(e)));
p.on("console", (m) => m.type() === "error" && errors.push(m.text()));
const res = await p.goto(process.argv[2]);
await p.waitForFunction(() => window.qrViewer, null, { timeout: 20000 }).catch((e) => errors.push("viewer missing: " + e.message));
const cache = res.headers()["cache-control"];
const info = await p.evaluate(() => document.getElementById("info")?.textContent);
console.log(JSON.stringify({ status: res.status(), cache, errors, info }));
await b.close();
