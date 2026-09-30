/** Image results use authenticated local files or HTTP provider results. */
export function generatedImageUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value || value.length > 8192) return undefined;
  if (value.startsWith('/api/generated-image/')) {
    const name = value.slice('/api/generated-image/'.length);
    return /^[A-Za-z0-9_-]+\.[A-Za-z0-9]+$/.test(name) ? value : undefined;
  }
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? value : undefined;
  } catch {
    return undefined;
  }
}
