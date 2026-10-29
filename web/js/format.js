// qrencode format v2, decoding side. Mirrors qrencode.py / qrdecode.py; see FORMAT.md.
// Runs in browsers and in Node 20+ (used by the tests).
import { decompress as zstdDecompress } from "../vendor/fzstd.mjs";

export const PACKET_VERSION = 2;
const PACKET_HEAD = 15; // version 1, file_id 4, stream_len 4, block_size 2, seq 4
const MAGIC = [0x51, 0x54, 0x46, 0x32]; // "QTF2"
const FLAG_ENCRYPTED = 1;
const STORE = 0, DEFLATE = 1, ZSTD = 2;

export class WrongPassword extends Error {}

// ------------------------------------------------------------------ crc32 (same as zlib)

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(bytes, end = bytes.length) {
  let c = 0xffffffff;
  for (let i = 0; i < end; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// ------------------------------------------------------------------ packets

/** Parse one QR payload. Returns null if it is not a valid v2 packet (wrong CRC, foreign code...). */
export function parsePacket(raw) {
  if (raw.length < PACKET_HEAD + 4) return null;
  const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  const end = raw.length - 4;
  if (crc32(raw, end) !== dv.getUint32(end)) return null;
  if (raw[0] !== PACKET_VERSION) return null;
  const blockSize = dv.getUint16(9);
  if (end - PACKET_HEAD !== blockSize) return null;
  return {
    fileId: raw.slice(1, 5),
    fileIdHex: hex(raw.subarray(1, 5)),
    streamLen: dv.getUint32(5),
    blockSize,
    seq: dv.getUint32(11),
    payload: raw.slice(PACKET_HEAD, end),
  };
}

export const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

// ------------------------------------------------------------------ fountain code

export function mulberry32(seed) {
  let a = seed | 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return (t ^ (t >>> 14)) >>> 0;
  };
}

/** Blocks XORed into packet `seq`, as a little-endian bitmask in 32-bit words. */
export function coefficients(fileId, seq, k) {
  const words = new Uint32Array((k + 31) >>> 5);
  if (seq < k) {
    words[seq >>> 5] = 1 << (seq & 31);
    return words;
  }
  const fid = ((fileId[0] << 24) | (fileId[1] << 16) | (fileId[2] << 8) | fileId[3]) >>> 0;
  const next = mulberry32((fid ^ Math.imul(seq + 1, 0x9e3779b9)) >>> 0);
  for (let w = 0; w < words.length; w++) words[w] = next();
  const tail = k & 31;
  if (tail) words[words.length - 1] &= ((1 << tail) >>> 0) - 1;
  if (words.every((w) => w === 0)) words[(seq % k) >>> 5] = 1 << ((seq % k) & 31);
  return words;
}

function lowestBit(words) {
  for (let w = 0; w < words.length; w++) {
    const x = words[w];
    if (x) return w * 32 + 31 - Math.clz32(x & -x);
  }
  return -1;
}

/** Online Gaussian elimination over GF(2): feed packets in any order and amount. */
export class FountainDecoder {
  constructor(fileId, streamLen, blockSize) {
    this.fileId = fileId;
    this.streamLen = streamLen;
    this.blockSize = blockSize;
    this.k = Math.max(1, Math.ceil(streamLen / blockSize));
    this.rows = new Array(this.k).fill(null); // pivot -> {mask, data}; pivot = lowest set bit
    this.rank = 0;
    this.seen = new Set();
  }

  get complete() {
    return this.rank === this.k;
  }

  /** Returns true if the packet carried new information. */
  add(seq, payload) {
    if (this.complete || this.seen.has(seq)) return false;
    this.seen.add(seq);
    const mask = coefficients(this.fileId, seq, this.k);
    const data = payload.slice();
    for (;;) {
      const pivot = lowestBit(mask);
      if (pivot < 0) return false;
      const row = this.rows[pivot];
      if (!row) {
        this.rows[pivot] = { mask, data };
        this.rank++;
        return true;
      }
      for (let i = 0; i < mask.length; i++) mask[i] ^= row.mask[i];
      for (let i = 0; i < data.length; i++) data[i] ^= row.data[i];
    }
  }

  /** The reassembled stream (call once complete). */
  stream() {
    const solved = new Array(this.k);
    for (let p = this.k - 1; p >= 0; p--) {
      const { mask, data } = this.rows[p];
      const out = data.slice();
      for (let w = 0; w < mask.length; w++) {
        let x = mask[w];
        while (x) {
          const b = 31 - Math.clz32(x & -x);
          x &= x - 1;
          const j = w * 32 + b;
          if (j !== p) {
            const s = solved[j];
            for (let i = 0; i < out.length; i++) out[i] ^= s[i];
          }
        }
      }
      solved[p] = out;
    }
    const all = new Uint8Array(this.k * this.blockSize);
    solved.forEach((s, i) => all.set(s, i * this.blockSize));
    return all.subarray(0, this.streamLen);
  }
}

// ------------------------------------------------------------------ container

export function isEncrypted(stream) {
  return (stream[4] & FLAG_ENCRYPTED) !== 0;
}

async function decrypt(stream, password) {
  const subtle = globalThis.crypto.subtle;
  const dv = new DataView(stream.buffer, stream.byteOffset, stream.byteLength);
  const iterations = dv.getUint32(5);
  const salt = stream.slice(9, 25), iv = stream.slice(25, 37);
  const base = await subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
  const key = await subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations },
    base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  try {
    const plain = await subtle.decrypt(
      { name: "AES-GCM", iv, additionalData: stream.slice(0, 37) }, key, stream.slice(37));
    return new Uint8Array(plain);
  } catch {
    throw new WrongPassword("wrong password");
  }
}

async function inflateRaw(bytes) {
  const ds = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(ds).arrayBuffer());
}

/** stream -> {name, data}. Throws WrongPassword, or Error if anything fails to verify. */
export async function unpack(stream, password = null) {
  if (!MAGIC.every((b, i) => stream[i] === b)) throw new Error("not a qrencode stream (or a newer version)");
  const inner = isEncrypted(stream) ? await decrypt(stream, password ?? "") : stream.subarray(5);
  const dv = new DataView(inner.buffer, inner.byteOffset, inner.byteLength);
  const method = inner[0], size = dv.getUint32(1), digest = inner.subarray(5, 37), nameLen = inner[37];
  const name = new TextDecoder().decode(inner.subarray(38, 38 + nameLen));
  const payload = inner.subarray(38 + nameLen);
  let data;
  if (method === STORE) data = payload.slice();
  else if (method === DEFLATE) data = await inflateRaw(payload);
  else if (method === ZSTD) data = zstdDecompress(payload);
  else throw new Error(`unknown compression method ${method}`);
  const actual = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", data));
  if (data.length !== size || !actual.every((b, i) => b === digest[i])) {
    throw new Error("SHA-256 mismatch: reconstructed file is corrupt");
  }
  return { name, data };
}

// ------------------------------------------------------------------ assembler

/** Collects packets from any number of frames; keeps files apart by file id. */
export class Assembler {
  constructor() {
    this.files = new Map(); // fileIdHex -> FountainDecoder
    this.rejected = 0;
  }

  /** Feed raw QR payloads. Returns {fresh, fileIdHex} where fileIdHex is the file that got data. */
  feed(rawCodes) {
    let fresh = 0, last = null;
    for (const raw of rawCodes) {
      const p = parsePacket(raw);
      if (!p) {
        this.rejected++;
        continue;
      }
      let dec = this.files.get(p.fileIdHex);
      if (!dec) {
        dec = new FountainDecoder(p.fileId, p.streamLen, p.blockSize);
        this.files.set(p.fileIdHex, dec);
      }
      if (dec.add(p.seq, p.payload)) fresh++;
      last = p.fileIdHex;
    }
    return { fresh, fileIdHex: last };
  }
}
