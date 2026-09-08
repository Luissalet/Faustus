import {ApiError, getJson} from './api';

export interface ArtifactInfo {
  id: string; label: string; kind: string; sha256: string; byteSize: number;
  mediaType: string; projectId: string; runId: string; sessionId: string; partial: boolean; createdAt: string;
}
export async function loadArtifactInfo(id: string, signal?: AbortSignal): Promise<ArtifactInfo> {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(id)) throw new Error('Invalid artifact ID');
  const body = await getJson<{ok?:boolean; artifact?:Record<string,unknown>}>(`/api/artifacts/${encodeURIComponent(id)}`, signal);
  const row = body.artifact;
  if (body.ok !== true || !row || typeof row.id !== 'string') throw new ApiError('Invalid artifact metadata', 502);
  const text = (key:string) => typeof row[key] === 'string' ? (row[key] as string).slice(0,2048) : '';
  return {id:text('id'), label:text('label'), kind:text('kind'), sha256:text('sha256'),
    byteSize:typeof row.byte_size === 'number' && Number.isSafeInteger(row.byte_size) && row.byte_size >= 0 ? row.byte_size : 0,
    mediaType:text('media_type'), projectId:text('project_id'), runId:text('run_id'), sessionId:text('session_id'),
    partial:row.partial === true, createdAt:text('created_at')};
}
