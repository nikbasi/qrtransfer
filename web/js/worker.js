// Finds QR codes in a frame, off the main thread so the camera preview stays smooth.
// Colour frames are split into their R/G/B QR layers first (same method as qrdecode.py).
import { prepareZXingModule, readBarcodes } from "../vendor/zxing/es/reader/index.js";

const ready = prepareZXingModule({
  overrides: {
    locateFile: (path, prefix) =>
      path.endsWith(".wasm") ? new URL("../vendor/zxing/reader/zxing_reader.wasm", import.meta.url).href : prefix + path,
  },
  fireImmediately: true,
});

const OPTIONS = { formats: ["QRCode"], maxNumberOfSymbols: 255, tryHarder: true };

// observed = C @ true; green leaks into both neighbours, red and blue mostly into green
const CROSSTALK_LEVELS = [0.0, 0.25, 0.45];
const UNMIX = CROSSTALK_LEVELS.map((a) => invert3([
  [1 - a, 0.75 * a, 0.25 * a],
  [0.5 * a, 1 - a, 0.5 * a],
  [0.25 * a, 0.75 * a, 1 - a],
]));
let preferredLevel = 0; // live mode starts with the level that worked last

function invert3(m) {
  const [[a, b, c], [d, e, f], [g, h, i]] = m;
  const A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
  const det = a * A + b * B + c * C;
  return [
    [A / det, -(b * i - c * h) / det, (b * f - c * e) / det],
    [B / det, (a * i - c * g) / det, -(a * f - c * d) / det],
    [C / det, -(a * h - b * g) / det, (a * e - b * d) / det],
  ];
}

async function scan(gray, width, height, found, binarizer) {
  const img = toImageData(gray, width, height);
  const results = await readBarcodes(img, binarizer ? { ...OPTIONS, binarizer } : OPTIONS);
  let valid = 0;
  for (const r of results) {
    if (r.isValid) {
      found.set(hexKey(r.bytes), r.bytes);
      valid++;
    }
  }
  return valid;
}

const hexKey = (b) => Array.prototype.map.call(b, (x) => x.toString(16).padStart(2, "0")).join("");

// Frames are large; reuse buffers between them instead of allocating ~50 MB per frame.
const buffers = new Map();
function buffer(name, Type, size) {
  let b = buffers.get(name);
  if (!b || b.length !== size) buffers.set(name, (b = new Type(size)));
  return b;
}

let grayImage = null;
function toImageData(gray, width, height) {
  if (gray instanceof ImageData) return gray;
  if (!grayImage || grayImage.width !== width || grayImage.height !== height) {
    grayImage = new ImageData(width, height);
    grayImage.data.fill(255); // alpha
  }
  const rgba = grayImage.data;
  for (let i = 0, j = 0; i < gray.length; i++, j += 4) rgba[j] = rgba[j + 1] = rgba[j + 2] = gray[i];
  return grayImage;
}

function isColorful(rgba, width, height) {
  let sat = 0, n = 0;
  for (let y = 0; y < height; y += 8) {
    for (let x = 0; x < width; x += 8) {
      const i = (y * width + x) * 4;
      const r = rgba[i], g = rgba[i + 1], b = rgba[i + 2];
      if (Math.max(r, g, b) - Math.min(r, g, b) > 60) sat++;
      n++;
    }
  }
  return sat / n > 0.02;
}

// ---- local normalisation: (v - localMin) / (localMax - localMin), windows blurred

function minmax1d(src, dst, n, r, isMax, g, h) {
  // van Herk / Gil-Werman: O(n) running max/min over a window of 2r+1
  const w = 2 * r + 1;
  for (let s = 0; s < n; s += w) {
    const e = Math.min(s + w, n);
    g[s] = src[s];
    for (let i = s + 1; i < e; i++) g[i] = isMax ? Math.max(g[i - 1], src[i]) : Math.min(g[i - 1], src[i]);
    h[e - 1] = src[e - 1];
    for (let i = e - 2; i >= s; i--) h[i] = isMax ? Math.max(h[i + 1], src[i]) : Math.min(h[i + 1], src[i]);
  }
  for (let i = 0; i < n; i++) {
    const a = h[Math.max(0, i - r)], b = g[Math.min(n - 1, i + r)];
    dst[i] = isMax ? Math.max(a, b) : Math.min(a, b);
  }
}

function box1d(src, dst, n, r) {
  let sum = 0, lo = 0, hi = -1;
  for (let i = 0; i < n; i++) {
    const a = Math.max(0, i - r), b = Math.min(n - 1, i + r);
    while (hi < b) sum += src[++hi];
    while (lo < a) sum -= src[lo++];
    dst[i] = sum / (b - a + 1);
  }
}

/** Apply a 1-D filter along rows then columns, in place. */
function separable(img, width, height, fn) {
  const n = Math.max(width, height);
  const line = new Float32Array(n), out = new Float32Array(n), s1 = new Float32Array(n), s2 = new Float32Array(n);
  for (let y = 0; y < height; y++) {
    const row = img.subarray(y * width, (y + 1) * width);
    fn(row, out, width, s1, s2);
    row.set(out.subarray(0, width));
  }
  for (let x = 0; x < width; x++) {
    for (let y = 0; y < height; y++) line[y] = img[y * width + x];
    fn(line, out, height, s1, s2);
    for (let y = 0; y < height; y++) img[y * width + x] = out[y];
  }
}

const BLOCK = 4; // envelopes are smooth, so they are computed on a 4x4-block grid and interpolated

function normalise(rgba, width, height, win) {
  const n = width * height;
  const bw = Math.ceil(width / BLOCK), bh = Math.ceil(height / BLOCK), nb = bw * bh;
  const rb = Math.max(1, Math.round((win >> 1) / BLOCK));
  // horizontal interpolation weights, shared by every row
  const x0 = new Int32Array(width), x1 = new Int32Array(width), fx = new Float32Array(width);
  for (let x = 0; x < width; x++) {
    const gx = Math.min(bw - 1, Math.max(0, (x + 0.5) / BLOCK - 0.5));
    x0[x] = Math.floor(gx);
    x1[x] = Math.min(bw - 1, x0[x] + 1);
    fx[x] = gx - x0[x];
  }
  const norm = [0, 1, 2].map((c) => buffer(`norm${c}`, Float32Array, n));
  for (let c = 0; c < 3; c++) {
    const hi = new Float32Array(nb).fill(0), lo = new Float32Array(nb).fill(255);
    for (let y = 0; y < height; y++) {
      const by = ((y / BLOCK) | 0) * bw;
      for (let x = 0, i = y * width * 4 + c; x < width; x++, i += 4) {
        const v = rgba[i], b = by + ((x / BLOCK) | 0);
        if (v > hi[b]) hi[b] = v;
        if (v < lo[b]) lo[b] = v;
      }
    }
    separable(hi, bw, bh, (s, d, len, g, h) => minmax1d(s, d, len, rb, true, g, h));
    separable(lo, bw, bh, (s, d, len, g, h) => minmax1d(s, d, len, rb, false, g, h));
    separable(hi, bw, bh, (s, d, len) => box1d(s, d, len, rb));
    separable(lo, bw, bh, (s, d, len) => box1d(s, d, len, rb));
    const out = norm[c];
    for (let y = 0; y < height; y++) {
      const gy = Math.min(bh - 1, Math.max(0, (y + 0.5) / BLOCK - 0.5));
      const r0 = Math.floor(gy) * bw, r1 = Math.min(bh - 1, Math.floor(gy) + 1) * bw, fy = gy - Math.floor(gy);
      for (let x = 0, i = y * width, j = y * width * 4 + c; x < width; x++, i++, j += 4) {
        const a = x0[x], b = x1[x], f = fx[x];
        const h0 = hi[r0 + a] + (hi[r0 + b] - hi[r0 + a]) * f, h1 = hi[r1 + a] + (hi[r1 + b] - hi[r1 + a]) * f;
        const l0 = lo[r0 + a] + (lo[r0 + b] - lo[r0 + a]) * f, l1 = lo[r1 + a] + (lo[r1 + b] - lo[r1 + a]) * f;
        const H = h0 + (h1 - h0) * fy, L = l0 + (l1 - l0) * fy;
        const v = (rgba[j] - L) / Math.max(H - L, 25);
        out[i] = v < 0 ? 0 : v > 1 ? 1 : v;
      }
    }
  }
  return norm;
}

function unmixLayers(norm, M, n) {
  const l0 = buffer("layer0", Uint8Array, n), l1 = buffer("layer1", Uint8Array, n), l2 = buffer("layer2", Uint8Array, n);
  const [R, G, B] = norm;
  const [[a, b, c], [d, e, f], [g, h, k]] = M;
  for (let i = 0; i < n; i++) {
    const r = R[i], gg = G[i], bb = B[i];
    l0[i] = a * r + b * gg + c * bb > 0.5 ? 255 : 0;
    l1[i] = d * r + e * gg + f * bb > 0.5 ? 255 : 0;
    l2[i] = g * r + h * gg + k * bb > 0.5 ? 255 : 0;
  }
  return [l0, l1, l2];
}

let rawFirst = true; // live mode: whichever pass completed the last frame goes first

async function readColor(img, mode, found, timing) {
  const { data: rgba, width, height } = img;
  const n = width * height;
  let t = performance.now();
  const lap = (key) => {
    const now = performance.now();
    timing[key] = (timing[key] || 0) + now - t;
    t = now;
  };
  const perLayer = [0, 0, 0];
  // all three layers agree: nothing more to find in this frame
  const settled = () => perLayer[0] > 0 && perLayer[0] === perLayer[1] && perLayer[1] === perLayer[2];
  const record = (counts) => counts.forEach((v, c) => (perLayer[c] = Math.max(perLayer[c], v)));

  async function rawPass() {
    const ch = buffer("channel", Uint8Array, n);
    const counts = [];
    for (let c = 0; c < 3; c++) {
      for (let i = 0; i < n; i++) ch[i] = rgba[i * 4 + c];
      counts.push(await scan(ch, width, height, found));
    }
    record(counts);
    lap("raw");
    return settled();
  }

  async function normalisedPass() {
    const longSide = Math.max(width, height);
    const windows = mode === "live" ? [Math.max(15, longSide >> 6) | 1]
      : [Math.max(15, longSide >> 6) | 1, Math.max(31, Math.floor(longSide / 24)) | 1];
    const levels = mode === "live" ? [preferredLevel] : [0, 1, 2];
    for (const win of windows) {
      const norm = normalise(rgba, width, height, win);
      lap("normalise");
      for (const lv of levels) {
        const layers = unmixLayers(norm, UNMIX[lv], n);
        lap("unmix");
        const counts = [];
        for (let c = 0; c < 3; c++) counts.push(await scan(layers[c], width, height, found));
        record(counts);
        lap("scan");
        if (settled()) {
          preferredLevel = lv;
          return true;
        }
      }
    }
    if (mode === "live") preferredLevel = (preferredLevel + 1) % UNMIX.length; // try another next frame
    return false;
  }

  if (mode !== "live" || rawFirst) {
    if (await rawPass()) return;
    if (await normalisedPass()) rawFirst = false;
  } else {
    if (await normalisedPass()) return;
    if (await rawPass()) rawFirst = true;
  }
}

self.onmessage = async ({ data: msg }) => {
  const t0 = performance.now();
  try {
    await ready;
    const img = new ImageData(new Uint8ClampedArray(msg.buffer), msg.width, msg.height);
    const found = new Map(), timing = {};
    const colorful = isColorful(img.data, img.width, img.height);
    if (colorful) {
      await readColor(img, msg.mode, found, timing);
    } else {
      await scan(img, img.width, img.height, found);
      if (msg.mode === "photo") await scan(img, img.width, img.height, found, "GlobalHistogram");
    }
    self.postMessage({ id: msg.id, codes: [...found.values()], colorful, timing, ms: performance.now() - t0 });
  } catch (e) {
    self.postMessage({ id: msg.id, codes: [], error: String(e), ms: performance.now() - t0 });
  }
};
