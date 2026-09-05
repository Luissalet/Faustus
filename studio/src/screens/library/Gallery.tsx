import { Check, CheckSquare, Download, FolderPlus, Heart, Images, Pencil, Plus, Shuffle, Sparkles, Trash2, Upload, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, Menu, Skeleton, Toast } from '../../components';
import {
  addToAlbum,
  aiTagBatch,
  clearAiTags,
  clearUserTags,
  createAlbum,
  dedupeTags,
  deleteAlbum,
  deleteImage,
  downloadZip,
  galleryStats,
  listAlbums,
  loadGallery,
  patchImage,
  removeFromAlbum,
  updateAlbum,
  uploadImages,
  type Album,
  type GalleryImage,
  type GalleryStats,
} from '../../adapters/gallery';
import { relativeTime } from '../../adapters/home';
import { ImageViewer } from './Viewer';
import { t, tn } from '../../i18n';

/**
 * The images of the Library (the previous Gallery). A grid that stays a
 * grid — pictures are judged by looking — with the filters as chips,
 * albums as a shelf, and one image at a time in the viewer. Selection
 * turns the grid into a tray for album, zip and delete.
 */

const PAGE = 48;

export function ImageGallery({ query, say }: { query: string; say: (m: string) => void }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [items, setItems] = useState<GalleryImage[] | null>(null);
  const [total, setTotal] = useState(0);
  const [tags, setTags] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [albums, setAlbums] = useState<Album[]>([]);
  const [stats, setStats] = useState<GalleryStats | null>(null);
  const [seed] = useState(() => Math.floor(Math.random() * 1e6));
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [albumDialog, setAlbumDialog] = useState<{ id?: string; name: string; description: string } | null>(null);
  const [confirm, setConfirm] = useState<{ kind: 'delete'; ids: string[] } | { kind: 'album'; id: string } | { kind: 'tags'; what: 'user' | 'ai' } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const album = params.get('album') ?? '';
  const tagFilter = (params.get('tag') ?? '').split(',').filter(Boolean);
  const model = params.get('model') ?? '';
  const favorites = params.get('fav') === '1';
  const sort = (params.get('sort') ?? 'recent') as 'recent' | 'oldest' | 'shuffle';
  const openId = params.get('img');

  const setParam = (key: string, value: string) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        return next;
      },
      { replace: true },
    );
  };

  const reload = useCallback(
    async (append = false) => {
      try {
        const page = await loadGallery({ search: query, tag: tagFilter, model, album, favorites, sort, seed, offset: append ? (items?.length ?? 0) : 0, limit: PAGE });
        setItems((prev) => (append && prev ? [...prev, ...page.items] : page.items));
        setTotal(page.total);
        setTags(page.tags);
        setModels(page.models);
      } catch (e) {
        say((e as Error).message);
        setItems((prev) => prev ?? []);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [query, params.get('tag'), model, album, favorites, sort, seed, say],
  );

  const reloadAlbums = useCallback(() => {
    void listAlbums().then(setAlbums).catch(() => setAlbums([]));
    void galleryStats().then(setStats).catch(() => setStats(null));
  }, []);

  useEffect(() => {
    setItems(null);
    void reload();
  }, [reload]);
  useEffect(reloadAlbums, [reloadAlbums]);

  const openIndex = useMemo(() => (openId && items ? items.findIndex((x) => x.id === openId) : -1), [openId, items]);
  const current = openIndex >= 0 && items ? items[openIndex] : null;

  const act = async (key: string, fn: () => Promise<void>, done?: string) => {
    setBusy(key);
    try {
      await fn();
      await reload();
      reloadAlbums();
      if (done) say(done);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const upload = async (files: File[]) => {
    const images = files.filter((f) => /^(image|video)\//.test(f.type));
    if (!images.length) return;
    setProgress({ done: 0, total: images.length });
    const r = await uploadImages(images, album || undefined, (done, all) => setProgress({ done, total: all }));
    setProgress(null);
    await reload();
    reloadAlbums();
    say(r.failed.length ? t('Uploaded {n}; {m} failed: {names}', { n: r.uploaded, m: r.failed.length, names: r.failed.join(', ') }) : tn(r.uploaded, 'Uploaded {n} image', 'Uploaded {n} images'));
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    void upload(Array.from(e.dataTransfer.files ?? []));
  };

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const remove = async (ids: string[]) => {
    setBusy('delete');
    let n = 0;
    for (const id of ids) {
      try {
        await deleteImage(id);
        n++;
      } catch {
        /* counted */
      }
    }
    setConfirm(null);
    setSelected(new Set());
    setSelecting(false);
    if (openId && ids.includes(openId)) setParam('img', '');
    await reload();
    reloadAlbums();
    setBusy(null);
    say(tn(n, 'Deleted {n} image', 'Deleted {n} images'));
  };

  const zip = async (ids: string[]) => {
    setBusy('zip');
    try {
      const blob = await downloadZip(ids);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `gallery-${new Date().toISOString().slice(0, 10)}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const currentAlbum = albums.find((a) => a.id === album) ?? null;

  return (
    <div className="fs-gal" data-dragging={dragging || undefined} onDragOver={(e) => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={onDrop} data-testid="gallery">
      <div className="fs-gal__toolbar">
        <div className="fs-gal__chips" role="group" aria-label={t('Filter images')}>
          <button type="button" className="fs-chip" data-on={favorites || undefined} onClick={() => setParam('fav', favorites ? '' : '1')} data-testid="gallery-fav">
            <Heart size={12} aria-hidden="true" /> {t('Favourites')}
            {stats ? <span className="fs-gal__n">{stats.favorites}</span> : null}
          </button>
          {albums.map((a) => (
            <button key={a.id} type="button" className="fs-chip" data-on={album === a.id || undefined} onClick={() => setParam('album', album === a.id ? '' : a.id)} data-testid={`gallery-album-${a.id}`}>
              {a.name} <span className="fs-gal__n">{a.count}</span>
            </button>
          ))}
          <button type="button" className="fs-chip fs-gal__chip-new" onClick={() => setAlbumDialog({ name: '', description: '' })} data-testid="gallery-new-album">
            <Plus size={12} aria-hidden="true" /> {t('Album')}
          </button>
        </div>
        <span className="fs-gal__spacer" />
        {models.length > 1 && (
          <select className="fs-field" value={model} onChange={(e) => setParam('model', e.target.value)} aria-label={t('Model')}>
            <option value="">{t('Any model')}</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        )}
        <select className="fs-field" value={sort} onChange={(e) => setParam('sort', e.target.value === 'recent' ? '' : e.target.value)} aria-label={t('Sort')}>
          <option value="recent">{t('Newest first')}</option>
          <option value="oldest">{t('Oldest first')}</option>
          <option value="shuffle">{t('Shuffled')}</option>
        </select>
        <Button variant="ghost" size="sm" icon={Upload} label={t('Upload')} onClick={() => fileRef.current?.click()} testId="gallery-upload" />
        <input ref={fileRef} type="file" accept="image/*,video/*" multiple hidden onChange={(e) => void upload(Array.from(e.target.files ?? []))} />
        <Button
          variant="ghost"
          size="sm"
          icon={selecting ? X : CheckSquare}
          label={selecting ? t('Leave selection') : t('Select several')}
          onClick={() => {
            setSelecting((v) => !v);
            setSelected(new Set());
          }}
          testId="gallery-select"
        />
        <Menu
          trigger={<Button variant="ghost" size="sm" icon={Sparkles} label={t('Tags')} />}
          align="end"
          items={[
            { label: t('Tag every untagged image with AI'), onSelect: () => void act('aitag', async () => { const r = await aiTagBatch(); say(tn(r.queued, '{n} image queued for tagging', '{n} images queued for tagging')); }) },
            { label: t('Merge duplicate tags'), onSelect: () => void act('dedupe', dedupeTags, t('Tags merged')) },
            null,
            { label: t('Clear my tags on every image'), variant: 'danger', onSelect: () => setConfirm({ kind: 'tags', what: 'user' }) },
            { label: t('Clear the AI tags on every image'), variant: 'danger', onSelect: () => setConfirm({ kind: 'tags', what: 'ai' }) },
          ]}
        />
      </div>

      {tags.length > 0 && (
        <div className="fs-gal__tags" role="group" aria-label={t('Tags')}>
          {tags.slice(0, 40).map((x) => (
            <button key={x} type="button" className="fs-gal__tag" data-on={tagFilter.includes(x) || undefined} onClick={() => setParam('tag', (tagFilter.includes(x) ? tagFilter.filter((y) => y !== x) : [...tagFilter, x]).join(','))}>
              {x}
            </button>
          ))}
          {tagFilter.length > 0 && <Button variant="ghost" size="sm" icon={X} label={t('Clear tags')} onClick={() => setParam('tag', '')} />}
        </div>
      )}

      {currentAlbum && (
        <div className="fs-gal__album-head">
          <div>
            <h3>{currentAlbum.name}</h3>
            {currentAlbum.description && <p className="fs-gal__muted">{currentAlbum.description}</p>}
          </div>
          <div className="fs-gal__row">
            <Button variant="ghost" size="sm" icon={Pencil} label={t('Rename')} onClick={() => setAlbumDialog({ id: currentAlbum.id, name: currentAlbum.name, description: currentAlbum.description })} />
            <Button variant="danger" size="sm" icon={Trash2} label={t('Delete album')} onClick={() => setConfirm({ kind: 'album', id: currentAlbum.id })} />
          </div>
        </div>
      )}

      {selecting && (
        <div className="fs-gal__bulk" role="toolbar" aria-label={t('Selection')}>
          <label className="fs-switch">
            <input type="checkbox" checked={!!items?.length && items.every((x) => selected.has(x.id))} onChange={(e) => setSelected(e.target.checked ? new Set((items ?? []).map((x) => x.id)) : new Set())} />
            <span>{t('All')}</span>
          </label>
          <span className="fs-gal__muted">{tn(selected.size, '{n} selected', '{n} selected#')}</span>
          <span className="fs-gal__spacer" />
          {albums.length > 0 && (
            <Menu
              trigger={<Button variant="ghost" size="sm" icon={FolderPlus} label={t('Add to album')} disabled={!selected.size} />}
              items={albums.map((a) => ({ label: a.name, onSelect: () => void act('album', () => addToAlbum(a.id, [...selected]), t('Added to {name}', { name: a.name })) }))}
            />
          )}
          {album && <Button variant="ghost" size="sm" icon={X} label={t('Remove from album')} disabled={!selected.size} onClick={() => void act('album', () => removeFromAlbum(album, [...selected]), t('Removed'))} />}
          <Button variant="ghost" size="sm" icon={Heart} label={t('Favourite')} disabled={!selected.size} onClick={() => void act('fav', async () => { for (const id of selected) await patchImage(id, { favorite: true }); })} />
          <Button variant="ghost" size="sm" icon={Download} label={t('Download zip')} disabled={!selected.size} loading={busy === 'zip'} onClick={() => void zip([...selected])} />
          <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} disabled={!selected.size} onClick={() => setConfirm({ kind: 'delete', ids: [...selected] })} />
        </div>
      )}

      {progress && (
        <p className="fs-gal__progress" role="status">
          {t('Uploading {done} of {total}…', { done: progress.done, total: progress.total })}
        </p>
      )}

      {items === null ? (
        <div className="fs-grid">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <Skeleton key={i} label={t('Loading the images')} height="200px" radius="panel" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          icon={Images}
          headingLevel={3}
          title={query || album || tagFilter.length || favorites || model ? t('Nothing matches the filter') : t('No images yet')}
          body={query || album || tagFilter.length || favorites || model ? t('Try another text, tag or album.') : t('Generate one in Studio, or drop your photos here.')}
          primaryAction={{ label: t('Upload'), icon: Upload, onClick: () => fileRef.current?.click() }}
        />
      ) : (
        <>
          <div className="fs-grid fs-gal__grid">
            {items.map((img) => (
              <div key={img.id} className="fs-gal__cell" data-selected={selected.has(img.id) || undefined}>
                {selecting && <input type="checkbox" className="fs-gal__check" checked={selected.has(img.id)} onChange={() => toggle(img.id)} aria-label={t('Select {name}', { name: img.filename })} />}
                <button type="button" className="fs-card fs-gal__card" onClick={() => (selecting ? toggle(img.id) : setParam('img', img.id))} data-testid="gallery-card">
                  <img className="fs-card__preview" src={img.url} alt={img.caption || img.prompt || img.filename} loading="lazy" decoding="async" width={img.width ?? undefined} height={img.height ?? undefined} />
                  {img.favorite && (
                    <span className="fs-gal__fav" aria-label={t('Favourite')}>
                      <Heart size={12} aria-hidden="true" />
                    </span>
                  )}
                  <span className="fs-card__body">
                    <span className="fs-card__title">{img.caption || img.prompt || img.filename}</span>
                    <span className="fs-card__meta">{[img.model, img.width && img.height ? `${img.width}×${img.height}` : null, relativeTime(img.createdAt)].filter(Boolean).join(' · ')}</span>
                  </span>
                </button>
              </div>
            ))}
          </div>
          {items.length < total && (
            <div className="fs-gal__more">
              <Button variant="secondary" size="sm" label={t('Show more ({n} left)', { n: total - items.length })} onClick={() => void reload(true)} />
            </div>
          )}
          <p className="fs-gal__muted fs-gal__stats">
            {tn(total, '{n} image', '{n} images')}
            {stats ? ` · ${stats.sizeHuman} · ${tn(stats.albums, '{n} album', '{n} albums')}` : ''}
            {sort === 'shuffle' && (
              <>
                {' · '}
                <Shuffle size={11} aria-hidden="true" /> {t('shuffled')}
              </>
            )}
          </p>
        </>
      )}

      {current && items && (
        <ImageViewer
          image={current}
          albums={albums}
          index={openIndex}
          count={items.length}
          onStep={(d) => {
            const next = items[openIndex + d];
            if (next) setParam('img', next.id);
          }}
          onClose={() => setParam('img', '')}
          onChanged={() => {
            void reload();
            reloadAlbums();
          }}
          onDelete={() => setConfirm({ kind: 'delete', ids: [current.id] })}
          onEdit={() => navigate(`/library/edit?img=${encodeURIComponent(current.id)}`)}
          say={say}
        />
      )}

      {albumDialog && (
        <Dialog
          open
          onOpenChange={(o) => !o && setAlbumDialog(null)}
          title={albumDialog.id ? t('Rename the album') : t('New album')}
          testId="gallery-album-dialog"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setAlbumDialog(null)} />
              <Button
                variant="primary"
                size="sm"
                label={albumDialog.id ? t('Save') : t('Create')}
                disabled={!albumDialog.name.trim()}
                loading={busy === 'album-save'}
                onClick={() =>
                  void act('album-save', async () => {
                    if (albumDialog.id) await updateAlbum(albumDialog.id, { name: albumDialog.name.trim(), description: albumDialog.description });
                    else {
                      const id = await createAlbum(albumDialog.name.trim(), albumDialog.description);
                      if (id) setParam('album', id);
                    }
                    setAlbumDialog(null);
                  }, albumDialog.id ? t('Album renamed') : t('Album created'))
                }
                testId="gallery-album-save"
              />
            </>
          }
        >
          <label className="fs-gal__field">
            <span>{t('Name')}</span>
            <input className="fs-field" value={albumDialog.name} onChange={(e) => setAlbumDialog({ ...albumDialog, name: e.target.value })} autoFocus data-testid="gallery-album-name" />
          </label>
          <label className="fs-gal__field">
            <span>{t('Description')}</span>
            <input className="fs-field" value={albumDialog.description} onChange={(e) => setAlbumDialog({ ...albumDialog, description: e.target.value })} />
          </label>
        </Dialog>
      )}

      {confirm?.kind === 'delete' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={tn(confirm.ids.length, 'Delete {n} image?', 'Delete {n} images?')}
          testId="gallery-confirm-delete"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button variant="danger-solid" size="sm" label={t('Delete')} loading={busy === 'delete'} onClick={() => void remove(confirm.ids)} testId="gallery-confirm-delete-ok" />
            </>
          }
        >
          <p className="fs-prose">{t('The file goes with it. This cannot be undone.')}</p>
        </Dialog>
      )}

      {confirm?.kind === 'album' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={t('Delete the album?')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="danger-solid"
                size="sm"
                label={t('Delete album')}
                loading={busy === 'album-del'}
                onClick={() =>
                  void act('album-del', async () => {
                    await deleteAlbum(confirm.id);
                    setConfirm(null);
                    setParam('album', '');
                  }, t('Album deleted'))
                }
              />
            </>
          }
        >
          <p className="fs-prose">{t('The images stay; only the album goes.')}</p>
        </Dialog>
      )}

      {confirm?.kind === 'tags' && (
        <Dialog
          open
          onOpenChange={(o) => !o && setConfirm(null)}
          title={confirm.what === 'user' ? t('Clear your tags on every image?') : t('Clear the AI tags on every image?')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="danger-solid"
                size="sm"
                label={t('Clear')}
                loading={busy === 'clear-tags'}
                onClick={() =>
                  void act('clear-tags', async () => {
                    await (confirm.what === 'user' ? clearUserTags() : clearAiTags());
                    setConfirm(null);
                  }, t('Cleared'))
                }
              />
            </>
          }
        >
          <p className="fs-prose">{t('This cannot be undone.')}</p>
        </Dialog>
      )}

      {notice && (
        <Toast>
          <Check size={12} aria-hidden="true" /> {notice}
        </Toast>
      )}
    </div>
  );
}
