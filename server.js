import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import process from 'node:process';

const port = process.env.PORT || '3080';
const host = process.env.HOST || '0.0.0.0';

// Tenta usar o binário local do DSH; se não existir, usa npx
const localBin = join(
  process.cwd(),
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'dsh.cmd' : 'dsh'
);

let command;
let args;

if (existsSync(localBin)) {
  command = localBin;
  args = ['web'];
} else {
  command = 'npx';
  args = ['--yes', '@deepseek-ai/dsh', 'web'];
}

const child = spawn(command, args, {
  stdio: 'inherit',
  env: { ...process.env, PORT: port, HOST: host },
  shell: process.platform === 'win32',
});

child.on('error', (err) => {
  console.error('Erro ao iniciar DSH:', err);
  process.exit(1);
});

child.on('exit', (code) => {
  console.log(`DSH saiu com código ${code}`);
  process.exit(code ?? 0);
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal));
}
