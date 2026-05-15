const fs = require('fs');
const path = require('path');
const sharp = require('sharp');

const SRC = __dirname;
const OUT = path.resolve(__dirname, '..');

const jobs = [
  { svg: 'icon-16.svg',  png: 'icon16.png',  size: 16  },
  { svg: 'icon-24.svg',  png: 'icon24.png',  size: 24  },
  { svg: 'icon-32.svg',  png: 'icon32.png',  size: 32  },
  { svg: 'icon-256.svg', png: 'icon48.png',  size: 48  },
  { svg: 'icon-256.svg', png: 'icon128.png', size: 128 },
  { svg: 'icon-256.svg', png: 'icon256.png', size: 256 },
];

(async () => {
  for (const j of jobs) {
    const inBuf = fs.readFileSync(path.join(SRC, j.svg));
    await sharp(inBuf, { density: 384 })
      .resize(j.size, j.size, { fit: 'contain', background: { r:0, g:0, b:0, alpha:0 } })
      .png({ compressionLevel: 9 })
      .toFile(path.join(OUT, j.png));
    const stat = fs.statSync(path.join(OUT, j.png));
    console.log(`${j.png.padEnd(13)} ${j.size}px  ${stat.size}b`);
  }
})();
