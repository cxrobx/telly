"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { CalendarRange, Home, Search, Settings, Sparkles, Tv } from "lucide-react";
import { BackdropProvider } from "./backdrop";
import { PlexbotChat } from "./plexbot";

const TABS = [
  { href: "/", label: "Home", icon: Home },
  { href: "/timeline", label: "Timeline", icon: CalendarRange },
  { href: "/for-you", label: "For You", icon: Sparkles },
  { href: "/following", label: "Following", icon: Tv },
];

const PUBLIC = ["/signin", "/link"];

function active(path: string, href: string) {
  return href === "/" ? path === "/" : path.startsWith(href);
}

export function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const bare = PUBLIC.some((p) => path.startsWith(p));
  return (
    <BackdropProvider>
      {!bare && (
        <header className="topbar">
          <div className="topbar-inner">
            <Link href="/" className="brand" aria-label="Telly home">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/logo.png" alt="" className="logo-mark" width={34} height={34} />
              <span className="wordmark">Telly</span>
            </Link>
            <nav className="tabs" aria-label="Main">
              {TABS.map((t) => (
                <Link key={t.href} href={t.href} className="tab" data-active={active(path, t.href)}>
                  {t.label}
                </Link>
              ))}
            </nav>
            <div className="topbar-actions">
              <Link href="/following?search=1" className="icon-btn" aria-label="Search shows">
                <Search size={18} />
              </Link>
              <Link href="/settings" className="icon-btn" aria-label="Settings" data-active={path.startsWith("/settings")}>
                <Settings size={18} />
              </Link>
            </div>
          </div>
        </header>
      )}
      <main className={bare ? "main-bare" : "main"}>{children}</main>
      {!bare && <PlexbotChat />}
      {!bare && (
        <nav className="bottombar" aria-label="Main">
          {TABS.map((t) => (
            <Link key={t.href} href={t.href} className="bottom-tab" data-active={active(path, t.href)}>
              <t.icon size={20} />
              <span>{t.label}</span>
            </Link>
          ))}
        </nav>
      )}
    </BackdropProvider>
  );
}
