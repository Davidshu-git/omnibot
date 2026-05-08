import { useState, useCallback } from "react";

interface Props {
  id: string;
  truncate?: number;
  className?: string;
}

export default function CopyableId({ id, truncate, className }: Props) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard unavailable
    }
  }, [id]);

  const display = truncate && id.length > truncate
    ? id.slice(0, Math.floor(truncate / 2)) + "…" + id.slice(-Math.floor(truncate / 2))
    : id;

  return (
    <span
      className={`copyable-id${copied ? " copied" : ""}${className ? " " + className : ""}`}
      onClick={copy}
      title={copied ? "已复制" : id}
    >
      {display}
      <span style={{ fontSize: 10, opacity: 0.55 }}>{copied ? "✓" : "⧉"}</span>
    </span>
  );
}
