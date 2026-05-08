import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useState, type ReactNode } from "react";

const NAV = [
  { label: "总览",   href: "/",         icon: "◈" },
  { label: "会话",   href: "/sessions", icon: "◉" },
  { label: "Token",  href: "/tokens",   icon: "◎" },
  { label: "工具",   href: "/tools",    icon: "◆" },
];

export default function Layout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 768px)");
    const update = () => setCollapsed(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  const w = collapsed ? 48 : 180;

  return (
    <div style={{ display: "flex", minHeight: "100vh" }}>
      <nav
        className="sidebar"
        style={{
          width: w,
          flexShrink: 0,
          background: "var(--surface)",
          borderRight: "1px solid var(--border)",
          display: "flex",
          flexDirection: "column",
          transition: `width var(--dur) var(--ease)`,
          overflow: "hidden",
        }}
      >
        {/* logo */}
        <div style={{
          padding: collapsed ? "1rem 0" : "1.25rem 1.25rem 1rem",
          borderBottom: "1px solid var(--border)",
          display: "flex",
          alignItems: "center",
          justifyContent: collapsed ? "center" : "flex-start",
          gap: 8,
          flexShrink: 0,
        }}>
          <span style={{
            background: "linear-gradient(135deg, rgba(96,165,250,.2), rgba(196,181,253,.2))",
            border: "1px solid rgba(96,165,250,.25)",
            borderRadius: 6,
            width: 26,
            height: 26,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 12,
            color: "var(--blue)",
            flexShrink: 0,
          }}>◈</span>
          <span className="sidebar-logo-text" style={{
            color: "var(--blue)",
            fontSize: 13,
            fontWeight: 700,
            letterSpacing: "0.02em",
            whiteSpace: "nowrap",
          }}>
            AgentObs
          </span>
        </div>

        {/* nav */}
        <div style={{ flex: 1, padding: collapsed ? "0.75rem 0" : "0.75rem" }}>
          {NAV.map((n) => {
            const active = router.pathname === n.href;
            return (
              <Link
                key={n.href}
                href={n.href}
                title={collapsed ? n.label : undefined}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: collapsed ? "center" : "flex-start",
                  gap: 8,
                  padding: collapsed ? "8px 0" : "7px 10px",
                  borderRadius: "var(--r)",
                  marginBottom: 2,
                  color: active ? "var(--blue)" : "var(--text-muted)",
                  background: active ? "var(--blue-dim)" : "transparent",
                  fontWeight: active ? 600 : 400,
                  fontSize: 13,
                  transition: `background var(--dur) var(--ease), color var(--dur) var(--ease)`,
                  textDecoration: "none",
                }}
                onMouseEnter={(e) => {
                  if (!active) {
                    const el = e.currentTarget as HTMLElement;
                    el.style.background = "var(--border)";
                    el.style.color = "var(--text)";
                  }
                }}
                onMouseLeave={(e) => {
                  if (!active) {
                    const el = e.currentTarget as HTMLElement;
                    el.style.background = "transparent";
                    el.style.color = "var(--text-muted)";
                  }
                }}
              >
                <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", flexShrink: 0 }}>
                  {n.icon}
                </span>
                <span className="sidebar-label" style={{ whiteSpace: "nowrap" }}>
                  {n.label}
                </span>
              </Link>
            );
          })}
        </div>

        {/* footer */}
        <div
          className="sidebar-footer"
          style={{
            padding: "0.75rem 1.25rem",
            borderTop: "1px solid var(--border)",
            color: "var(--text-dim)",
            fontSize: 10,
            whiteSpace: "nowrap",
          }}
        >
          Agent Observability
        </div>
      </nav>

      <main style={{
        flex: 1,
        padding: "1.75rem 2rem",
        overflowY: "auto",
        background: "var(--bg)",
        minWidth: 0,
      }}>
        {children}
      </main>
    </div>
  );
}
