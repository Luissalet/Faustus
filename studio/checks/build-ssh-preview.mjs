import {build} from 'esbuild';
await build({entryPoints:['studio/checks/ssh-trust-preview.tsx'],bundle:true,jsx:'automatic',format:'esm',
  outfile:process.argv[2] || '.impeccable/review/ssh-trust-preview/app.js',define:{'process.env.NODE_ENV':'"development"'}});
