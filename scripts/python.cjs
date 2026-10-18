const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const python = path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
if (!fs.existsSync(python)) {
  console.error('Missing project virtualenv. Run uv sync --python 3.12 from Genesis.');
  process.exit(1);
}
const child = spawnSync(python, process.argv.slice(2), { cwd: root, stdio: 'inherit' });
if (child.error) console.error('Unable to start the project Python environment.');
process.exit(child.status ?? 1);
