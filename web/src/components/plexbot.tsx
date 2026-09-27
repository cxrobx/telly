"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { MessageCircle, X } from "lucide-react";

// plexbot's web chat, the same one Overseerr embeds. Telly signs the token itself
// (backend plexbot.py), for the member's own Overseerr account, so the chat, its history and
// its admin rights are the ones they already have there. No Overseerr account → no button.
type Grant = { token: string; id: string; name: string; chat_url: string; expires_in: number };

// Re-mint on open once the token is this close to expiring (it lives an hour).
const REFRESH_MARGIN_S = 600;

async function fetchGrant(): Promise<Grant | null> {
  // A plain fetch, not api(): this must never redirect or throw on the pages it decorates.
  try {
    const res = await fetch("/api/plexbot-token", { credentials: "same-origin" });
    return res.ok ? ((await res.json()) as Grant) : null;
  } catch {
    return null;
  }
}

function chatSrc(g: Grant) {
  const params = new URLSearchParams({ token: g.token, userId: g.id, displayName: g.name });
  return `${g.chat_url}${g.chat_url.includes("?") ? "&" : "?"}${params}`;
}

export function PlexbotChat() {
  const [grant, setGrant] = useState<(Grant & { at: number }) | null>(null);
  const [open, setOpen] = useState(false);
  // The iframe mounts on first open and stays, so closing keeps the conversation.
  const [mounted, setMounted] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let live = true;
    fetchGrant().then((g) => live && g && setGrant({ ...g, at: Date.now() }));
    return () => { live = false; };
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    buttonRef.current?.focus();
  }, []);

  const toggle = useCallback(async () => {
    if (open) return close();
    if (grant && Date.now() - grant.at > (grant.expires_in - REFRESH_MARGIN_S) * 1000) {
      const g = await fetchGrant();
      if (g) setGrant({ ...g, at: Date.now() });
    }
    setMounted(true);
    setOpen(true);
  }, [open, grant, close]);

  // The chat's own ✕ posts {type: "plexbot-close"} to its parent.
  useEffect(() => {
    if (!grant) return;
    const origin = new URL(grant.chat_url).origin;
    const onMessage = (e: MessageEvent) => {
      if (e.origin === origin && (e.data as { type?: string } | null)?.type === "plexbot-close") close();
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [grant, close]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  if (!grant) return null;
  return (
    <>
      {mounted && (
        <div className="plexbot-panel glass" data-open={open} aria-hidden={!open}>
          <iframe src={chatSrc(grant)} title="plexbot" referrerPolicy="origin" allow="clipboard-write" />
        </div>
      )}
      <button
        ref={buttonRef}
        type="button"
        className="plexbot-fab"
        data-open={open}
        aria-expanded={open}
        aria-label={open ? "Close plexbot" : "Chat with plexbot"}
        onClick={toggle}
      >
        {open ? <X size={22} /> : <MessageCircle size={22} />}
      </button>
    </>
  );
}
