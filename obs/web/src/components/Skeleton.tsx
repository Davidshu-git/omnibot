export function SkeletonLine({
  width = "100%",
  height = 12,
  style,
}: {
  width?: string | number;
  height?: number;
  style?: React.CSSProperties;
}) {
  return (
    <div
      className="skeleton"
      style={{ width, height, borderRadius: "var(--r-sm)", marginBottom: 8, ...style }}
    />
  );
}

export function SkeletonCard() {
  return (
    <div className="card" style={{ minWidth: 280, position: "relative" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "1.25rem" }}>
        <div>
          <SkeletonLine width={120} height={14} style={{ marginBottom: 6 }} />
          <SkeletonLine width={72} height={10} style={{ marginBottom: 0 }} />
        </div>
        <SkeletonLine width={44} height={26} style={{ marginBottom: 0, borderRadius: "var(--r-sm)" }} />
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem", marginBottom: "1rem" }}>
        {[...Array(4)].map((_, i) => (
          <div key={i}>
            <SkeletonLine width="50%" height={9} style={{ marginBottom: 4 }} />
            <SkeletonLine width="70%" height={16} style={{ marginBottom: 0 }} />
          </div>
        ))}
      </div>
      <SkeletonLine width="100%" height={4} style={{ marginBottom: 0, borderRadius: 2 }} />
    </div>
  );
}

export function SkeletonSessionItem() {
  return (
    <div style={{
      padding: "8px 10px",
      borderRadius: "var(--r)",
      marginBottom: 4,
      background: "var(--surface)",
      border: "1px solid var(--border)",
      borderLeft: "3px solid var(--border-hi)",
    }}>
      <SkeletonLine width="60%" height={10} style={{ marginBottom: 5 }} />
      <SkeletonLine width="80%" height={9} style={{ marginBottom: 4 }} />
      <SkeletonLine width="45%" height={8} style={{ marginBottom: 0 }} />
    </div>
  );
}
