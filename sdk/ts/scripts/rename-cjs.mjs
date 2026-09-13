#!/usr/bin/env node
// Renames every .js tsc emitted under a directory to .cjs, and rewrites the
// relative require("./x.js") specifiers it wrote (source imports use the
// NodeNext ".js" convention so the same .ts sources also build a real ESM
// tree) to point at the renamed ".cjs" sibling. Node's CommonJS loader does
// not append ".cjs" for an extensionless or ".js" require, so the rename
// alone would leave every cross-file require() dangling — this rewrites
// them too. No dependencies: two filesystem walks and a regex.
import { readdirSync, statSync, readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';

const root = process.argv[2];
if (!root) {
  console.error('usage: rename-cjs.mjs <dir>');
  process.exit(1);
}

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walk(full));
    else if (name.endsWith('.js')) out.push(full);
  }
  return out;
}

for (const file of walk(root)) {
  const rewritten = readFileSync(file, 'utf8').replace(
    /require\((['"])(\.\.?\/[^'"]+)\.js\1\)/g,
    (_m, quote, spec) => `require(${quote}${spec}.cjs${quote})`,
  );
  writeFileSync(file.slice(0, -3) + '.cjs', rewritten);
  unlinkSync(file);
}
