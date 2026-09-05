import { Brain, Pencil, Save, X } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import { relativeTime } from '../../adapters/home';
import { formatBytes, getMemory, readMemoryFile, scaffoldMemory, writeMemoryFile, type Project, type ProjectMemory } from '../../adapters/projects';
import { t } from '../../i18n';

/**
 * The project memory: text files in its folder that the agent reads when
 * it works here. Open one to read it, edit it in place, save.
 */
export function ProjectMemoryFiles({ project, say }: { project: Project; say: (m: string) => void }) {
  const [memory, setMemory] = useState<ProjectMemory | null | undefined>(undefined);
  const [open, setOpen] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(() => {
    getMemory(project.id)
      .then(setMemory)
      .catch(() => setMemory(null));
  }, [project.id]);
  useEffect(reload, [reload]);

  const openFile = async (name: string) => {
    setOpen(name);
    setText(null);
    setDraft(null);
    try {
      setText(await readMemoryFile(project.id, name));
    } catch (e) {
      say((e as Error).message);
      setOpen(null);
    }
  };

  const save = async () => {
    if (!open || draft === null) return;
    setSaving(true);
    try {
      await writeMemoryFile(project.id, open, draft);
      setText(draft);
      setDraft(null);
      say(t('{name} saved', { name: open }));
      reload();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (memory === undefined) return <Skeleton label={t('Loading the memory')} count={3} height="36px" />;

  return (
    <div className="fs-pj__memory">
      <p className="fs-panel__label">{memory?.dir ?? t('Project memory')}</p>
      {memory && memory.files.length > 0 ? (
        <div className="fs-files">
          {memory.files.map((file) => (
            <button type="button" className="fs-file fs-pj__file" key={file.name} onClick={() => void openFile(file.name)} aria-current={open === file.name || undefined} data-testid="memory-file">
              <span className="fs-file__name">{file.name}</span>
              <span className="fs-file__meta">{formatBytes(file.size)}</span>
              <span className="fs-file__meta">{relativeTime(file.modified)}</span>
            </button>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={Brain}
          headingLevel={3}
          title={t('No memory yet')}
          body={project.workspace ? t('The project memory is text files in its folder. Faustus reads them when it works here, and you can edit them like any other file.') : t('Set a working folder first; the memory lives inside it.')}
          primaryAction={project.workspace ? { label: t('Create the memory files'), onClick: () => void scaffoldMemory(project.id).then(reload, (e: Error) => say(e.message)) } : undefined}
        />
      )}

      {open && (
        <section className="fs-pj__memfile" aria-label={open}>
          <header className="fs-pj__memfile-head">
            <code>{open}</code>
            <div className="fs-pj__row">
              {draft === null ? (
                <Button variant="secondary" size="sm" icon={Pencil} label={t('Edit')} disabled={text === null} onClick={() => setDraft(text ?? '')} testId="memory-edit" />
              ) : (
                <>
                  <Button variant="primary" size="sm" icon={Save} label={t('Save')} loading={saving} onClick={() => void save()} testId="memory-save" />
                  <Button variant="ghost" size="sm" icon={X} label={t('Cancel')} onClick={() => setDraft(null)} />
                </>
              )}
              <Button variant="ghost" size="sm" icon={X} label={t('Close')} onClick={() => setOpen(null)} />
            </div>
          </header>
          {text === null ? (
            <p className="fs-pj__muted">{t('Loading…')}</p>
          ) : draft === null ? (
            <pre className="fs-pj__pre" data-testid="memory-text">{text || t('(empty)')}</pre>
          ) : (
            <textarea className="fs-field fs-pj__textarea fs-pj__textarea--code" value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} rows={18} data-testid="memory-editor" />
          )}
        </section>
      )}
    </div>
  );
}
