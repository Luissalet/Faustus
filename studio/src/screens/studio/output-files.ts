import type {Turn} from './model';

/** Only completed writes are results. A proposal, denial or failed write is not an artifact. */
export function outputFiles(turns:Turn[]):string[] {
  const paths = new Set<string>();
  const add = (path:unknown) => {
    if(typeof path === 'string' && path.trim() && path.length < 4096) paths.add(path.trim());
  };
  for(const turn of turns) for(const step of turn.steps) {
    if(step.state !== 'succeeded') continue;
    add(step.diff?.file);
    if(!/^(write_file|edit_file|create_file|transform_media)$/.test(step.tool)) continue;
    for(const raw of [step.command, step.output]) {
      try {
        const data:unknown = JSON.parse(raw || '');
        if(data && typeof data === 'object' && !Array.isArray(data)) add((data as {path?:unknown}).path);
      } catch { /* Legacy file tools use a path on the first line. */ }
    }
    if(step.command && !step.command.trim().startsWith('{') && step.tool !== 'transform_media')
      add(step.command.split('\n')[0]);
  }
  return [...paths];
}
