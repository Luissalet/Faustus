import { Circle, Eraser, Minus, Pen, RotateCcw, Trash2, Type } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { IconButton } from '../../components';
import { t } from '../../i18n';
import { CANVAS_H, CANVAS_W, INKS, PAPER, TEXT_SIZES, WIDTHS, isDrag, pointIn, pushUndo, radius, safeImage, type Point, type Size, type Tool } from '../../lib/paint';

/**
 * A note you draw.
 *
 * The previous interface kept `eraser`, `text` and `line` as separate
 * booleans, which is how it ended up in states where the text tool looked
 * broken after using the eraser (its own comment says so). Here there is
 * one `tool`, so there is one truth.
 *
 * The photo, when there is one, is painted onto the canvas underneath so a
 * drawing over a photo is one picture, which is what gets saved.
 */

const TOOLS: { id: Tool; icon: typeof Pen; label: string }[] = [
  { id: 'pen', icon: Pen, label: 'Pen' },
  { id: 'eraser', icon: Eraser, label: 'Eraser' },
  { id: 'line', icon: Minus, label: 'Line' },
  { id: 'circle', icon: Circle, label: 'Circle' },
  { id: 'text', icon: Type, label: 'Text' },
];

/**
 * The canvas belongs to whoever saves it, so the parent passes its own ref
 * in. A callback prop would be a new function on every render, the setup
 * effect would run again, and the drawing would be wiped the moment
 * somebody typed a title — which is exactly what happened once.
 */
export function Draw({ photo, canvasRef }: { photo: string | null; canvasRef: RefObject<HTMLCanvasElement | null> }) {
  const ref = canvasRef;
  const [tool, setTool] = useState<Tool>('pen');
  const [ink, setInk] = useState<string>(INKS[0]);
  const [size, setSize] = useState<Size>('m');
  const [undo, setUndo] = useState<ImageData[]>([]);
  const [typing, setTyping] = useState<(Point & { screen: Point }) | null>(null);
  const [text, setText] = useState('');
  const drag = useRef<{ from: Point; before: ImageData | null } | null>(null);

  const context = () => ref.current?.getContext('2d') ?? null;

  const blank = useCallback((ctx: CanvasRenderingContext2D) => {
    ctx.fillStyle = PAPER;
    ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
  }, []);

  // Set the surface up once, then paint the photo (if any) into it: a
  // drawing over a photo is one picture and saves as one PNG.
  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    blank(ctx);
    const src = safeImage(photo);
    if (!src) return;
    const image = new Image();
    image.crossOrigin = 'anonymous';
    image.onload = () => {
      // Contain, so a photo of any shape keeps its proportions on the note.
      const scale = Math.min(CANVAS_W / image.width, CANVAS_H / image.height);
      const w = image.width * scale;
      const h = image.height * scale;
      try {
        ctx.drawImage(image, (CANVAS_W - w) / 2, (CANVAS_H - h) / 2, w, h);
      } catch {
        /* a photo that will not paint is not worth an exception */
      }
    };
    image.src = src;
  }, [photo, blank, ref]);

  const snapshot = (): ImageData | null => {
    const ctx = context();
    try {
      return ctx ? ctx.getImageData(0, 0, CANVAS_W, CANVAS_H) : null;
    } catch {
      return null;
    }
  };

  const remember = () => {
    const shot = snapshot();
    if (shot) setUndo((stack) => pushUndo(stack, shot));
  };

  const stepBack = () => {
    const ctx = context();
    if (!ctx || !undo.length) return;
    ctx.putImageData(undo[undo.length - 1], 0, 0);
    setUndo((stack) => stack.slice(0, -1));
  };

  const clear = () => {
    const ctx = context();
    if (!ctx) return;
    remember();
    blank(ctx);
  };

  const paint = (ctx: CanvasRenderingContext2D) => {
    ctx.strokeStyle = tool === 'eraser' ? PAPER : ink;
    ctx.fillStyle = tool === 'eraser' ? PAPER : ink;
    ctx.lineWidth = tool === 'eraser' ? WIDTHS[size] * 4 : WIDTHS[size];
  };

  const at = (event: { clientX: number; clientY: number }): Point | null => {
    const canvas = ref.current;
    if (!canvas) return null;
    return pointIn(canvas.getBoundingClientRect(), event.clientX, event.clientY);
  };

  const down = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const ctx = context();
    const point = at(event);
    if (!ctx || !point) return;
    if (tool === 'text') {
      const rect = ref.current?.getBoundingClientRect();
      setTyping({ ...point, screen: { x: event.clientX - (rect?.left ?? 0), y: event.clientY - (rect?.top ?? 0) } });
      setText('');
      return;
    }
    try {
      // A pointer that is already gone throws here, and losing the capture
      // must not lose the stroke.
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      /* draw without it */
    }
    remember();
    drag.current = { from: point, before: tool === 'pen' || tool === 'eraser' ? null : snapshot() };
    if (tool === 'pen' || tool === 'eraser') {
      paint(ctx);
      ctx.beginPath();
      ctx.moveTo(point.x, point.y);
      ctx.lineTo(point.x + 0.01, point.y);
      ctx.stroke();
    }
  };

  const move = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const ctx = context();
    const point = at(event);
    const from = drag.current;
    if (!ctx || !point || !from) return;
    paint(ctx);
    if (tool === 'pen' || tool === 'eraser') {
      ctx.lineTo(point.x, point.y);
      ctx.stroke();
      return;
    }
    // A shape redraws from the snapshot each move, so the preview never
    // leaves a trail behind it.
    if (from.before) ctx.putImageData(from.before, 0, 0);
    paint(ctx);
    ctx.beginPath();
    if (tool === 'line') {
      ctx.moveTo(from.from.x, from.from.y);
      ctx.lineTo(point.x, point.y);
    } else {
      ctx.arc(from.from.x, from.from.y, radius(from.from, point), 0, Math.PI * 2);
    }
    ctx.stroke();
  };

  const up = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const from = drag.current;
    drag.current = null;
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      /* the pointer went away */
    }
    const point = at(event);
    // A shape that was only a tap leaves nothing but an undo entry: take it back.
    if (from && from.before && point && !isDrag(from.from, point)) {
      const ctx = context();
      ctx?.putImageData(from.before, 0, 0);
      setUndo((stack) => stack.slice(0, -1));
    }
  };

  const commitText = () => {
    const ctx = context();
    const where = typing;
    setTyping(null);
    if (!ctx || !where || !text.trim()) return;
    remember();
    ctx.fillStyle = ink;
    ctx.textBaseline = 'top';
    // A canvas font string cannot read a CSS variable: it needs real names.
    ctx.font = `600 ${TEXT_SIZES[size]}px system-ui, -apple-system, Segoe UI, sans-serif`;
    ctx.fillText(text, where.x, where.y);
    setText('');
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        stepBack();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  return (
    <div className="fs-draw" data-testid="note-draw">
      <div className="fs-draw__wrap">
        <canvas
          ref={ref}
          className="fs-draw__canvas"
          width={CANVAS_W}
          height={CANVAS_H}
          data-tool={tool}
          aria-label={t('Drawing surface')}
          onPointerDown={down}
          onPointerMove={move}
          onPointerUp={up}
          onPointerCancel={up}
        />
        {typing && (
          <input
            className="fs-draw__type"
            autoFocus
            value={text}
            style={{ insetInlineStart: `${typing.screen.x}px`, insetBlockStart: `${typing.screen.y}px`, fontSize: `${TEXT_SIZES[size]}px` }}
            aria-label={t('Text to write')}
            onChange={(e) => setText(e.target.value)}
            onBlur={commitText}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                commitText();
              } else if (e.key === 'Escape') {
                e.preventDefault();
                setTyping(null);
                setText('');
              }
            }}
          />
        )}
      </div>
      <div className="fs-draw__bar">
        <div className="fs-draw__tools" role="group" aria-label={t('Tool')}>
          {TOOLS.map((entry) => (
            <IconButton key={entry.id} icon={entry.icon} label={t(entry.label)} size="sm" data-on={tool === entry.id || undefined} onClick={() => setTool(entry.id)} />
          ))}
        </div>
        <ul className="fs-draw__inks" aria-label={t('Ink')}>
          {INKS.map((colour) => (
            <li key={colour}>
              <button type="button" className="fs-draw__ink" data-on={ink === colour || undefined} style={{ background: colour }} aria-label={colour} title={colour} onClick={() => setInk(colour)} />
            </li>
          ))}
        </ul>
        <div className="fs-draw__sizes" role="group" aria-label={t('Size')}>
          {(['s', 'm', 'l'] as Size[]).map((key) => (
            <button key={key} type="button" className="fs-draw__size" data-on={size === key || undefined} onClick={() => setSize(key)} aria-label={t(key === 's' ? 'Small' : key === 'm' ? 'Medium' : 'Large')}>
              <span style={{ inlineSize: `${WIDTHS[key] + 2}px`, blockSize: `${WIDTHS[key] + 2}px` }} />
            </button>
          ))}
        </div>
        <div className="fs-draw__end">
          <IconButton icon={RotateCcw} label={t('Undo')} size="sm" disabled={!undo.length} onClick={stepBack} />
          <IconButton icon={Trash2} label={t('Clear the drawing')} size="sm" onClick={clear} />
        </div>
      </div>
    </div>
  );
}
