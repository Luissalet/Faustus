export interface SkeletonProps {
  /** Describe what is loading, not that something is. Read aloud on wait. */
  label: string;
  width?: string;
  height?: string;
  radius?: 'control' | 'panel' | 'preview' | 'pill';
  count?: number;
}

/**
 * A placeholder shaped like the content that will replace it. Anything
 * else is a loading spinner wearing a costume.
 */
export function Skeleton({
  label,
  width = '100%',
  height = '16px',
  radius = 'control',
  count = 1,
}: SkeletonProps) {
  return (
    <div
      role="status"
      aria-busy="true"
      aria-label={label}
      data-testid="skeleton"
      style={{ display: 'grid', gap: 'var(--fs-space-2)' }}
    >
      {Array.from({ length: count }, (_, index) => (
        <div
          key={index}
          className="fs-skeleton"
          style={{
            width,
            height,
            borderRadius: `var(--fs-radius-${radius})`,
          }}
        />
      ))}
    </div>
  );
}
