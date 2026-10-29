import { Assembler, WrongPassword, isEncrypted, unpack } from "./format.js";

const $ = (id) => document.getElementById(id);
const video = $("video"), viewport = $("viewport"), bar = $("bar"), statusEl = $("status");
const startBtn = $("start"), photosInput = $("photos");

const worker = new Worker(new URL("./worker.js", import.meta.url), { type: "module" });
const pending = new Map();
let nextId = 0;
worker.onmessage = ({ data }) => {
  pending.get(data.id)?.(data);
  pending.delete(data.id);
};

/** Send pixels to the worker; resolves with {codes, colorful, ms}. */
function detect(imageData, mode) {
  const id = nextId++;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    const { width, height, data } = imageData;
    worker.postMessage({ id, mode, width, height, buffer: data.buffer }, [data.buffer]);
  });
}

const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d", { willReadFrequently: true });

function grab(source, srcW, srcH, maxSide) {
  const s = Math.min(1, maxSide / Math.max(srcW, srcH));
  canvas.width = Math.round(srcW * s);
  canvas.height = Math.round(srcH * s);
  ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
  return ctx.getImageData(0, 0, canvas.width, canvas.height);
}

// ------------------------------------------------------------------ state

let asm = new Assembler();
let stream = null, scanning = false, frames = 0, t0 = 0, finishing = false;

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle("err", isError);
}

function report(fileIdHex) {
  const dec = fileIdHex && asm.files.get(fileIdHex);
  if (!dec) {
    if (asm.rejected) setStatus("Seeing QR codes, but not ones made by qrencode.");
    return;
  }
  bar.style.width = `${(100 * dec.rank) / dec.k}%`;
  const fps = frames / Math.max(0.001, (performance.now() - t0) / 1000);
  setStatus(scanning
    ? `${dec.rank} / ${dec.k} codes · ${fps.toFixed(1)} frames/s`
    : `${dec.rank} / ${dec.k} codes. Add more photos (any pages).`);
}

function feed(codes) {
  if (!codes.length) return null;
  const { fresh, fileIdHex } = asm.feed(codes);
  if (fresh) {
    viewport.classList.add("flash");
    setTimeout(() => viewport.classList.remove("flash"), 120);
  }
  report(fileIdHex);
  const dec = fileIdHex && asm.files.get(fileIdHex);
  return dec?.complete ? dec : null;
}

// ------------------------------------------------------------------ camera

async function startCamera() {
  if (!navigator.mediaDevices?.getUserMedia) {
    setStatus("Camera not available here. The page must be opened over https.", true);
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
    });
  } catch (e) {
    setStatus(`Camera blocked: ${e.message}`, true);
    return;
  }
  const track = stream.getVideoTracks()[0];
  try {
    await track.applyConstraints({ advanced: [{ focusMode: "continuous" }] });
  } catch { /* not supported everywhere */ }
  video.srcObject = stream;
  await video.play().catch(() => {});
  viewport.classList.add("live");
  startBtn.textContent = "Stop camera";
  scanning = true;
  frames = 0;
  t0 = performance.now();
  setStatus("Point the camera at the slideshow…");
  loop();
}

function stopCamera() {
  scanning = false;
  stream?.getTracks().forEach((t) => t.stop());
  stream = null;
  video.srcObject = null;
  viewport.classList.remove("live");
  startBtn.textContent = "Start camera";
}

async function loop() {
  while (scanning) {
    if (video.readyState < 2 || !video.videoWidth) {
      await new Promise((r) => setTimeout(r, 50));
      continue;
    }
    const res = await detect(grab(video, video.videoWidth, video.videoHeight, 1920), "live");
    frames++;
    window.__lastTiming = { ms: res.ms, ...res.timing }; // inspected by tests
    if (!scanning) break;
    const done = feed(res.codes);
    if (done) {
      stopCamera();
      await finish(done);
    }
  }
}

// ------------------------------------------------------------------ photos

async function scanPhotos(files) {
  for (const [i, file] of [...files].entries()) {
    setStatus(`Reading photo ${i + 1} of ${files.length}…`);
    let bmp;
    try {
      bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      setStatus(`Could not open ${file.name}`, true);
      continue;
    }
    const res = await detect(grab(bmp, bmp.width, bmp.height, 3000), "photo");
    bmp.close();
    const done = feed(res.codes);
    if (done) return finish(done);
    if (!res.codes.length) setStatus(`No codes found in photo ${i + 1}. Try a sharper, closer shot.`, true);
  }
}

// ------------------------------------------------------------------ result

function askPassword(retry) {
  const dlg = $("pw-dialog"), input = $("pw-input");
  $("pw-error").hidden = !retry;
  input.value = "";
  return new Promise((resolve) => {
    $("pw-cancel").onclick = () => { dlg.close(); resolve(null); };
    $("pw-form").onsubmit = (e) => { e.preventDefault(); dlg.close(); resolve(input.value); };
    dlg.showModal();
    input.focus();
  });
}

async function finish(dec) {
  if (finishing) return;
  finishing = true;
  bar.classList.add("done");
  bar.style.width = "100%";
  navigator.vibrate?.(60);
  const data = dec.stream();
  try {
    let file = null, retry = false;
    while (!file) {
      let password = null;
      if (isEncrypted(data)) {
        setStatus("All codes read. Encrypted file.");
        password = await askPassword(retry);
        if (password === null) {
          setStatus("Cancelled. Scan again to retry.");
          return;
        }
        setStatus("Decrypting…");
      }
      try {
        file = await unpack(data, password);
      } catch (e) {
        if (!(e instanceof WrongPassword)) throw e;
        retry = true;
      }
    }
    showResult(file);
  } catch (e) {
    setStatus(`Failed: ${e.message}`, true);
  } finally {
    finishing = false;
  }
}

const MIME = {
  pdf: "application/pdf", txt: "text/plain", md: "text/markdown", csv: "text/csv", json: "application/json",
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", zip: "application/zip",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
};

function asText(data) {
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(data);
    const controls = (text.match(/[\x00-\x08\x0e-\x1f]/g) || []).length;
    return controls <= text.length / 100 ? text : null;
  } catch {
    return null;
  }
}

function formatSize(n) {
  return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(2)} MB`;
}

let blobUrl = null;

function showResult({ name, data }) {
  const ext = name.split(".").pop().toLowerCase();
  const type = MIME[ext] || "application/octet-stream";
  const file = new File([data], name, { type });
  if (blobUrl) URL.revokeObjectURL(blobUrl);
  blobUrl = URL.createObjectURL(file);
  const text = asText(data);

  $("scanner").hidden = true;
  $("result").hidden = false;
  $("r-name").textContent = name;
  $("r-meta").textContent = `${formatSize(data.length)} · verified`;
  $("r-text").hidden = text === null;
  $("r-text").textContent = text ?? "";

  $("r-save").onclick = () => {
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = name;
    a.click();
  };
  const canShare = navigator.canShare?.({ files: [file] });
  $("r-share").hidden = !canShare;
  $("r-share").onclick = () => navigator.share({ files: [file], title: name }).catch(() => {});
  $("r-copy").hidden = text === null || !navigator.clipboard;
  $("r-copy").onclick = async () => {
    await navigator.clipboard.writeText(text);
    $("r-copy").textContent = "Copied";
  };
  $("r-open").hidden = !/^(application\/pdf|image\/)/.test(type);
  $("r-open").onclick = () => window.open(blobUrl, "_blank");
  document.body.dataset.state = "done"; // for tests
}

function reset() {
  asm = new Assembler();
  bar.style.width = "0";
  bar.classList.remove("done");
  $("result").hidden = true;
  $("scanner").hidden = false;
  $("r-copy").textContent = "Copy text";
  delete document.body.dataset.state;
  setStatus("Ready");
}

// ------------------------------------------------------------------ wiring

startBtn.onclick = () => (scanning ? stopCamera() : startCamera());
photosInput.onchange = () => {
  if (photosInput.files.length) scanPhotos(photosInput.files).finally(() => (photosInput.value = ""));
};
$("r-again").onclick = reset;

if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
