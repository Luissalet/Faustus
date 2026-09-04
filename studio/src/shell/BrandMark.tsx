/**
 * The Faustus mark: the arrowhead and its two wings, lifted from the
 * favicon script in index.html so the shell carries the real identity and
 * not a text diamond standing in for it.
 *
 * This is the one inline vector the Studio tree allows itself. The guard in
 * tests/test_studio_guards.py forbids inline vectors so that icons come from
 * lucide-react; a brand mark is not an icon, and the exemption is stated on
 * the tag itself.
 */
export function BrandMark({ size = 30 }: { size?: number }) {
  return (
    <svg viewBox="0 0 32 32" width={size} height={size} className="fs-brand-mark" aria-hidden="true" data-note="guard-ok: brand mark, not an icon">
      <path
        fill="currentColor"
        d="M16 0.738L4.674 25.559L13.004 28.992L13.004 24.798L9.229 24.798L9.229 19.644L16 12.094L22.772 19.644L22.772 24.798L17.678 24.798L13.4 31.264L27.326 25.559Z"
      />
      <g fill="currentColor" opacity="0.62">
        <path d="M5.993 5.022L0.839 13.292L6.233 18.985L9.049 13.172L4.555 11.434Z" />
        <path d="M26.008 5.022L31.161 13.292L25.768 18.985L22.952 13.172L27.446 11.434Z" />
      </g>
    </svg>
  );
}
