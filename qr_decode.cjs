// Decode downloaded PNG/JPEG bytes or a browser PNG screenshot. stdout is JSON only.
'use strict';
const fs = require('node:fs');
const jsQR = require('jsqr');
const { PNG } = require('pngjs');
const jpeg = require('jpeg-js');
const MAX_PIXELS = 12 * 1024 * 1024;
try {
  const path = process.argv[2];
  if (!path || fs.statSync(path).size > 3 * 1024 * 1024) throw new Error('invalid image size');
  const bytes = fs.readFileSync(path);
  let image;
  if (bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) {
    if (bytes.length < 24 || bytes.readUInt32BE(16) * bytes.readUInt32BE(20) > MAX_PIXELS) throw new Error('image dimensions too large');
    image = PNG.sync.read(bytes, { checkCRC: true });
  } else if (bytes[0] === 0xff && bytes[1] === 0xd8) {
    image = jpeg.decode(bytes, { useTArray: true, maxResolutionInMP: 12, maxMemoryUsageInMB: 128 });
  } else throw new Error('only PNG and JPEG are supported');
  if (!image.width || !image.height || image.width * image.height > MAX_PIXELS) throw new Error('invalid image dimensions');
  const code = jsQR(new Uint8ClampedArray(image.data), image.width, image.height, { inversionAttempts: 'attemptBoth' });
  process.stdout.write(JSON.stringify(code ? [code.data] : []));
} catch (error) {
  process.stderr.write('QR image decoding failed: ' + error.message + '\n');
  process.exitCode = 1;
}
