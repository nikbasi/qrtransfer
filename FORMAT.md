# qrencode format, version 2

Three implementations must agree byte for byte: `qrencode.py` (writes),
`qrdecode.py` and `web/js/format.js` (read). The tests check them against
each other (`tests/test_js_format.py`, `tests/test_browser.py`). All integers
are big-endian.

## 1. Stream

The file is wrapped into one byte string, the *stream*.

```
magic   4   "QTF2"
flags   1   bit 0: encrypted
then either the inner block (not encrypted) or:
  iterations  4   PBKDF2 iterations (600000)
  salt       16
  iv         12
  ciphertext  …   AES-256-GCM(inner), 16-byte tag appended
```

Encryption key = PBKDF2-HMAC-SHA256(password as UTF-8, salt, iterations,
32 bytes). The GCM additional data is the 37 header bytes (magic to iv), so
the header can't be altered without detection. A wrong password and tampered
data both fail the tag check.

Inner block:

```
method     1   0 = stored, 1 = raw deflate (RFC 1951), 2 = zstd frame
size       4   original file size
sha256    32   SHA-256 of the original file
name_len   1
name       …   UTF-8 file name
payload    …   compressed file
```

The decoder checks both size and SHA-256 after decompressing.

## 2. Fountain code

The stream is zero-padded and cut into `k = ceil(len / block_size)` blocks.
Packet `seq` carries the XOR of the blocks selected by `coefficients(seq)`:

- `seq < k`: only block `seq` (the data packets).
- `seq >= k`: a random mask of `k` bits. Bit `i` of word `w` selects block
  `32*w + i`. The words come from mulberry32 seeded with
  `file_id_u32 XOR ((seq + 1) * 0x9E3779B9 mod 2^32)`. Bits at or above `k`
  are cleared; an all-zero mask becomes block `seq mod k`.

```
mulberry32(a):            # all arithmetic mod 2^32
  a = a + 0x6D2B79F5
  t = (a XOR a>>15) * (a OR 1)
  t = (t + (t XOR t>>7) * (t OR 61)) XOR t
  return t XOR t>>14
```

Reference: seed `0x12345678` gives 455919406, 4042750857, 4036713555.

Any `k` linearly independent packets rebuild the stream (Gaussian elimination
over GF(2)); in practice that's `k` plus about 2 packets.

## 3. Packet (one QR code, byte mode)

```
version     1   2
file_id     4   first 4 bytes of SHA-256(stream)
stream_len  4
block_size  2
seq         4
payload     block_size
crc32       4   zlib CRC-32 of all bytes above
```

## 4. Pages

Packets are drawn as QR codes with a 4-module quiet zone, filling the page
grid in order. In colour mode each slot holds three QR codes of the same
version. Packets `3i`, `3i+1` and `3i+2` go in the red, green and blue
channels: a channel is 0 where its QR module is dark and 255 where it is
light. The finder patterns come out black, so the codes are found like plain
QR codes.
