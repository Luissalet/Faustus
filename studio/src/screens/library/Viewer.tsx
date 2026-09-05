import { ChevronLeft, ChevronRight, Download, ExternalLink, Heart, MessageSquare, Pencil, RotateCw, Sparkles, Tag, Trash2, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import * as RadixDialog from '@radix-ui/react-dialog';
import { Button, IconButton } from '../../components';
import { aiTagImage, humanSize, patchImage, renameImage, rotateImage, toggleFavorite, type Album, type GalleryImage } from '../../adapters/gallery';
import { locale, t } from '../../i18n';

/**
 * One image, large, with everything about it beside it. A lightbox, not a
 * form: the picture leads, the inspector is quiet, arrows walk the set.
 */
export function ImageViewer({ image, albums, index, count, onStep, onClose, onChanged, onDelete, onEdit, say }: { image: GalleryImage; albums: Album[]; index: number; count: number; onStep: (delta: number) => void; onClose: () => void; onChanged: () => void; onDelete: () => void; onEdit: () => void; say: (m: string) => void }) {
  const navigate = useNavigate();
  const [tags, setTags] = useState(image.tags.join(', '));
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(image.filename);
  const [busy, setBusy] = useState<string | null>(null);
  const [rev, setRev] = useState(0);

  useEffect(() => {
    setTags(image.tags.join(', '));
    setName(image.filename);
    setRenaming(false);
  }, [image]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === 'ArrowLeft') onStep(-1);
      else if (e.key === 'ArrowRight') onStep(1);
      else if (e.key.toLowerCase() === 'e') onEdit();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onStep, onEdit]);

  const run = async (key: string, fn: () => Promise<void>, done?: string) => {
    setBusy(key);
    try {
      await fn();
      onChanged();
      if (done) say(done);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const saveTags = () => run('tags', () => patchImage(image.id, { tags }), t('Tags saved'));

  const toChat = async () => {
    if (image.sessionId) {
      navigate(`/studio?s=${encodeURIComponent(image.sessionId)}`);
      return;
    }
    navigate(`/studio?image=${encodeURIComponent(image.url)}&name=${encodeURIComponent(image.filename)}`);
  };

  const dims = image.width && image.height ? `${image.width}×${image.height}` : '';
  const src = `${image.url}${rev ? `${image.url.includes('?') ? '&' : '?'}r=${rev}` : ''}`;

  return (
    <RadixDialog.Root open onOpenChange={(o) => !o && onClose()}>
      <RadixDialog.Portal container={document.getElementById('fs-overlay-root') ?? undefined}>
        <RadixDialog.Overlay className="fs-overlay-backdrop" />
        <RadixDialog.Content className="fs-viewer" data-testid="image-viewer" aria-describedby={undefined}>
          <RadixDialog.Title className="fs-viewer__sr">{image.caption || image.prompt || image.filename}</RadixDialog.Title>
          <div className="fs-viewer__stage">
            <IconButton icon={ChevronLeft} label={t('Previous')} size="md" disabled={index <= 0} onClick={() => onStep(-1)} />
            <img className="fs-viewer__img" src={src} alt={image.caption || image.prompt || image.filename} width={image.width ?? undefined} height={image.height ?? undefined} />
            <IconButton icon={ChevronRight} label={t('Next')} size="md" disabled={index >= count - 1} onClick={() => onStep(1)} />
            <span className="fs-viewer__count">
              {index + 1} / {count}
            </span>
          </div>

          <aside className="fs-viewer__side">
            <header className="fs-viewer__head">
              {renaming ? (
                <form
                  className="fs-viewer__rename"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void run('rename', () => renameImage(image.id, name.trim()), t('Renamed')).then(() => setRenaming(false));
                  }}
                >
                  <input className="fs-field" value={name} onChange={(e) => setName(e.target.value)} aria-label={t('File name')} autoFocus />
                  <Button type="submit" variant="primary" size="sm" label={t('Save')} loading={busy === 'rename'} />
                  <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setRenaming(false)} />
                </form>
              ) : (
                <button type="button" className="fs-viewer__name" onClick={() => setRenaming(true)} title={t('Rename')}>
                  <code>{image.filename}</code>
                  <Pencil size={12} aria-hidden="true" />
                </button>
              )}
              <RadixDialog.Close asChild>
                <IconButton icon={X} label={t('Close')} size="sm" />
              </RadixDialog.Close>
            </header>

            <div className="fs-viewer__actions">
              {/* The pixel editor is its own lot (Z2); until it lands the button waits. */}
              <Button variant="primary" size="sm" icon={Pencil} label={t('Edit')} title={t('Open in the image editor (E)')} onClick={onEdit} testId="viewer-edit" />
              <Button variant={image.favorite ? 'secondary' : 'ghost'} size="sm" icon={Heart} label={image.favorite ? t('Favourite') : t('Favourite')} title={image.favorite ? t('Remove from favourites') : t('Add to favourites')} loading={busy === 'fav'} onClick={() => void run('fav', async () => void (await toggleFavorite(image.id)))} testId="viewer-favorite" />
              <Button variant="ghost" size="sm" icon={MessageSquare} label={image.sessionId ? t('Its chat') : t('To a chat')} title={image.sessionId ? t('Open the conversation it came from') : t('Start a conversation with this image attached')} onClick={() => void toChat()} testId="viewer-chat" />
              <Button
                variant="ghost"
                size="sm"
                icon={RotateCw}
                label={t('Rotate')}
                loading={busy === 'rotate'}
                onClick={() =>
                  void run('rotate', async () => {
                    await rotateImage(image.id, 90);
                    setRev((r) => r + 1);
                  })
                }
              />
              <Button
                variant="ghost"
                size="sm"
                icon={Download}
                label={t('Download')}
                onClick={() => {
                  const a = document.createElement('a');
                  a.href = image.url;
                  a.download = image.filename;
                  document.body.appendChild(a);
                  a.click();
                  a.remove();
                }}
              />
              <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} onClick={onDelete} testId="viewer-delete" />
            </div>

            {(image.caption || image.prompt) && (
              <div className="fs-viewer__block">
                <h3>{image.caption ? t('Caption') : t('Prompt')}</h3>
                <p>{image.caption || image.prompt}</p>
                {image.caption && image.prompt && <p className="fs-viewer__muted">{image.prompt}</p>}
              </div>
            )}

            <div className="fs-viewer__block">
              <h3>
                <Tag size={12} aria-hidden="true" /> {t('Tags')}
              </h3>
              <form
                className="fs-viewer__tags"
                onSubmit={(e) => {
                  e.preventDefault();
                  void saveTags();
                }}
              >
                <input className="fs-field" value={tags} onChange={(e) => setTags(e.target.value)} placeholder={t('comma-separated')} aria-label={t('Your tags')} data-testid="viewer-tags" />
                <Button type="submit" variant="secondary" size="sm" label={t('Save')} loading={busy === 'tags'} disabled={tags === image.tags.join(', ')} />
              </form>
              <div className="fs-viewer__ai">
                {image.aiTags.length > 0 ? (
                  <span className="fs-viewer__chips">
                    {image.aiTags.map((x) => (
                      <span key={x} className="fs-viewer__chip">
                        {x}
                      </span>
                    ))}
                  </span>
                ) : (
                  <span className="fs-viewer__muted">{t('No tags from the vision model yet.')}</span>
                )}
                <Button variant="ghost" size="sm" icon={Sparkles} label={t('Tag with AI')} loading={busy === 'ai'} onClick={() => void run('ai', async () => void (await aiTagImage(image.id)), t('Tagged'))} />
              </div>
            </div>

            <div className="fs-viewer__block">
              <h3>{t('Album')}</h3>
              <select className="fs-field" value={image.albumId} onChange={(e) => void run('album', () => patchImage(image.id, { album_id: e.target.value }), t('Moved'))} aria-label={t('Album')} data-testid="viewer-album">
                <option value="">{t('No album')}</option>
                {albums.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>

            <dl className="fs-viewer__facts">
              {image.model && (
                <div>
                  <dt>{t('Model')}</dt>
                  <dd>{image.model}</dd>
                </div>
              )}
              {(dims || image.fileSize) && (
                <div>
                  <dt>{t('Size')}</dt>
                  <dd>{[dims, humanSize(image.fileSize)].filter(Boolean).join(' · ')}</dd>
                </div>
              )}
              {image.createdAt && (
                <div>
                  <dt>{t('Added')}</dt>
                  <dd>{new Date(image.createdAt).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' })}</dd>
                </div>
              )}
              {image.takenAt && (
                <div>
                  <dt>{t('Taken')}</dt>
                  <dd>{new Date(image.takenAt).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' })}</dd>
                </div>
              )}
              {image.camera && (
                <div>
                  <dt>{t('Camera')}</dt>
                  <dd>{image.camera}</dd>
                </div>
              )}
              {image.gps && (
                <div>
                  <dt>{t('Location')}</dt>
                  <dd>
                    <a className="fs-link" href={`https://www.openstreetmap.org/?mlat=${image.gps.lat}&mlon=${image.gps.lng}#map=15/${image.gps.lat}/${image.gps.lng}`} target="_blank" rel="noopener noreferrer">
                      {image.gps.lat?.toFixed(4)}, {image.gps.lng?.toFixed(4)} <ExternalLink size={11} aria-hidden="true" />
                    </a>
                  </dd>
                </div>
              )}
              {image.sessionName && (
                <div>
                  <dt>{t('Conversation')}</dt>
                  <dd>{image.sessionName}</dd>
                </div>
              )}
              {image.quality && (
                <div>
                  <dt>{t('Quality')}</dt>
                  <dd>{image.quality}</dd>
                </div>
              )}
            </dl>
          </aside>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
