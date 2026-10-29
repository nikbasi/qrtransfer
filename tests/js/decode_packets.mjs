// Usage: node decode_packets.mjs input.json
// input: {"packets": [hex...], "password": str|null}. Prints {"name", "sha256", "used"} as JSON.
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { Assembler, isEncrypted, unpack } from "../../web/js/format.js";

const { packets, password } = JSON.parse(readFileSync(process.argv[2], "utf8"));
const asm = new Assembler();
let used = 0;
for (const h of packets) {
  asm.feed([Uint8Array.from(Buffer.from(h, "hex"))]);
  used++;
  const [dec] = asm.files.values();
  if (dec.complete) break;
}
const [dec] = asm.files.values();
if (!dec.complete) {
  console.log(JSON.stringify({ error: `incomplete ${dec.rank}/${dec.k}` }));
  process.exit(1);
}
const stream = dec.stream();
try {
  const { name, data } = await unpack(stream, isEncrypted(stream) ? password : null);
  console.log(JSON.stringify({ name, sha256: createHash("sha256").update(data).digest("hex"), used }));
} catch (e) {
  console.log(JSON.stringify({ error: e.constructor.name + ": " + e.message }));
  process.exit(1);
}
