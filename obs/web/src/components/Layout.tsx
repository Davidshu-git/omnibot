import type { ReactNode } from "react";
import { useIsMobile } from "@/lib/useIsMobile";

export default function Layout({ children }: { children: ReactNode }) {
  const isMobile = useIsMobile();

  return (
    <main style={{
      height: "100vh",
      padding: isMobile ? "1rem 0.875rem" : "1.75rem 2rem",
      overflowY: "auto",
      background: "var(--bg)",
    }}>
      {children}
    </main>
  );
}
