import {
  Copy,
  FolderOpen,
  Images,
  MoreHorizontal,
  Play,
  Plus,
  Sparkles,
  Sun,
  Trash2,
  Moon,
} from 'lucide-react';
import { useState, type ReactNode } from 'react';
import {
  Button,
  Dialog,
  EmptyState,
  ExecutionTrace,
  IconButton,
  Menu,
  Popover,
  Skeleton,
  StatusBadge,
  type RunStatus,
} from '../components';
import './gallery.css';

const SURFACES = [
  '--fs-canvas',
  '--fs-surface-1',
  '--fs-surface-2',
  '--fs-surface-3',
  '--fs-border',
  '--fs-border-strong',
];

const SEMANTIC = [
  '--fs-brand',
  '--fs-focus',
  '--fs-success',
  '--fs-warning',
  '--fs-danger',
  '--fs-info',
];

const STATUSES: RunStatus[] = [
  'queued',
  'running',
  'waiting',
  'paused',
  'succeeded',
  'failed',
  'cancelled',
];

function Section({
  title,
  layout,
  children,
}: {
  title: string;
  layout?: 'stack';
  children: ReactNode;
}) {
  return (
    <section className="fs-section">
      <h2 className="fs-section__title">{title}</h2>
      <div className="fs-section__body" data-layout={layout}>
        {children}
      </div>
    </section>
  );
}

function Swatches({ tokens }: { tokens: string[] }) {
  return (
    <div className="fs-swatches">
      {tokens.map((token) => (
        <div className="fs-swatch" key={token}>
          <div className="fs-swatch__chip" style={{ background: `var(${token})` }} />
          <span className="fs-swatch__name">{token}</span>
        </div>
      ))}
    </div>
  );
}

export function Gallery() {
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  const [dialogOpen, setDialogOpen] = useState(false);

  function toggleTheme() {
    const next = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    document.documentElement.setAttribute('data-theme', next);
  }

  return (
    <div className="fs-app fs-gallery">
      <a className="fs-skip-link" href="#gallery-main">
        Saltar al contenido
      </a>
      <div className="fs-gallery__inner" id="gallery-main">
        <header className="fs-gallery__head">
          <div>
            <h1 className="fs-gallery__title">Faustus Studio</h1>
            <p className="fs-gallery__sub">
              Primitivos del sistema de diseño. Cada valor sale de{' '}
              <code>DESIGN.md</code>; nada está escrito a mano.
            </p>
          </div>
          <Button
            variant="secondary"
            icon={theme === 'dark' ? Sun : Moon}
            label={theme === 'dark' ? 'Tema claro' : 'Tema oscuro'}
            onClick={toggleTheme}
          />
        </header>

        <Section title="Superficies y bordes">
          <Swatches tokens={SURFACES} />
        </Section>

        <Section title="Semántica">
          <Swatches tokens={SEMANTIC} />
        </Section>

        <Section title="Botones">
          <Button variant="primary" label="Nuevo trabajo" icon={Plus} />
          <Button variant="secondary" label="Abrir proyecto" icon={FolderOpen} />
          <Button variant="ghost" label="Ver receta" />
          <Button variant="danger" label="Eliminar" icon={Trash2} />
          <Button variant="danger-solid" label="Sí, eliminar" />
          <Button variant="primary" label="Generando" loading />
          <Button variant="secondary" label="No disponible" disabled />
          <Button variant="primary" size="sm" label="Pequeño" />
          <Button variant="primary" size="lg" label="Grande" />
        </Section>

        <Section title="Botones de icono">
          <IconButton icon={Play} label="Ejecutar" />
          <IconButton icon={Copy} label="Duplicar" variant="secondary" />
          <IconButton icon={Images} label="Galería" badge={3} />
          <IconButton icon={Sparkles} label="Variar" badge />
          <IconButton icon={Trash2} label="Eliminar" disabled />
        </Section>

        <Section title="Estados de run">
          {STATUSES.map((status) => (
            <StatusBadge key={status} status={status} />
          ))}
        </Section>

        <Section title="Traza de ejecución — elemento firma" layout="stack">
          <ExecutionTrace
            steps={[
              { id: 'ctx', label: 'Contexto: Campaña Lira · 3 referencias', state: 'succeeded', meta: '0,4 s' },
              { id: 'plan', label: 'Plan propuesto y aceptado', state: 'succeeded', meta: '2 s' },
              { id: 'refs', label: 'Preparando referencias', state: 'succeeded', meta: '11 s' },
              { id: 'gen', label: 'Generando 8 variaciones', state: 'running', meta: '62 % · 2 min' },
              { id: 'qa', label: 'Revisión de calidad', state: 'queued' },
              { id: 'pub', label: 'Aprobación para publicar', state: 'waiting' },
            ]}
          />
        </Section>

        <Section title="Menú, popover y diálogo">
          <Menu
            trigger={<IconButton icon={MoreHorizontal} label="Más acciones" />}
            items={[
              { label: 'Usar en…', icon: FolderOpen },
              { label: 'Variar', icon: Sparkles },
              { label: 'Duplicar', icon: Copy },
              null,
              { label: 'Eliminar', icon: Trash2, variant: 'danger' },
            ]}
          />
          <Popover trigger={<Button variant="secondary" label="Procedencia" />}>
            <p style={{ margin: 0, color: 'var(--fs-text-2)' }}>
              Generado por <strong>Vídeo corto v1</strong> sobre GPU local, a partir
              del brief y tres referencias.
            </p>
          </Popover>
          <Button variant="primary" label="Abrir diálogo" onClick={() => setDialogOpen(true)} />
          <Dialog
            open={dialogOpen}
            onOpenChange={setDialogOpen}
            title="Aprobar publicación"
            description="Faustus va a publicar el clip en el canal del proyecto. Nada sale de tu máquina hasta que lo apruebes."
            footer={
              <>
                <Button variant="ghost" label="Rechazar" onClick={() => setDialogOpen(false)} />
                <Button variant="primary" label="Aprobar" onClick={() => setDialogOpen(false)} />
              </>
            }
          />
        </Section>

        <Section title="Carga y vacío" layout="stack">
          <Skeleton label="Cargando proyectos" count={3} height="20px" />
          <EmptyState
            icon={Images}
            title="Todavía no hay nada aquí"
            body="Cuando termines un trabajo, sus resultados aparecerán en la biblioteca con su receta y su procedencia."
            primaryAction={{ label: 'Nuevo trabajo', icon: Plus }}
            secondaryAction={{ label: 'Ver ejemplos' }}
          />
        </Section>
      </div>
    </div>
  );
}
