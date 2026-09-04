/**
 * Stable test IDs (UI-011).
 *
 * The E2E suite drives the UI by these, so they must survive copy changes
 * and translation. Slugging the visible label is a compromise: it is
 * readable in a failing test, and any component may override it with an
 * explicit `testId` prop when the label is dynamic.
 */
export function slug(value: string): string {
  return value
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
}
