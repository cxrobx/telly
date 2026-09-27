"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";

function LinkFlow() {
  const params = useSearchParams();
  const code = params.get("code");
  const back = params.get("back");
  const [who, setWho] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "confirm" | "waiting" | "done" | "error">(
    !code ? "error" : back ? "waiting" : "loading",
  );
  const [msg, setMsg] = useState<string | null>(
    code ? null : "This link is missing its code. Say !link in #plexbot for a new one.",
  );
  const started = useRef(false);

  useEffect(() => {
    if (!code || started.current) return;
    started.current = true;
    if (!back) {
      api<{ discord_name: string }>("/link/describe", { body: { code } })
        .then((r) => {
          setWho(r.discord_name);
          setState("confirm");
        })
        .catch((e: Error) => {
          setState("error");
          setMsg(e.message);
        });
      return;
    }
    (async () => {
      for (let i = 0; i < 60; i++) {
        try {
          const r = await api<{ done: boolean; discord_name?: string; plex_username?: string }>("/link/finish", { body: { code } });
          if (r.done) {
            setState("done");
            setMsg(`Discord user ${r.discord_name} is now linked to Plex account ${r.plex_username}.`);
            return;
          }
        } catch (e) {
          setState("error");
          setMsg((e as Error).message);
          return;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
      setState("error");
      setMsg("Timed out waiting for Plex. Say !link again.");
    })();
  }, [code, back]);

  const go = async () => {
    try {
      const r = await api<{ auth_url: string }>("/link/start", { body: { code } });
      location.href = r.auth_url;
    } catch (e) {
      setState("error");
      setMsg((e as Error).message);
    }
  };

  return (
    <div className="glass signin">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/logo.png" alt="" className="logo-hero" width={112} height={112} />
      <div className="wordmark big">Telly</div>
      {state === "confirm" && (
        <>
          <p className="signin-lede">
            Link Discord user <strong>{who}</strong> to your Plex account?
          </p>
          <p className="muted small">Only continue if that&apos;s you. plexbot will then answer with your shows and DM you alerts.</p>
          <button className="btn btn-plex" onClick={go}>
            Sign in with Plex to link
          </button>
        </>
      )}
      {state === "loading" && <p className="muted">Checking your link…</p>}
      {state === "waiting" && <p className="muted">Finishing with Plex…</p>}
      {state === "done" && (
        <>
          <p className="signin-lede">You&apos;re linked.</p>
          <p className="muted">{msg} Ask plexbot &ldquo;what&apos;s coming up for me?&rdquo;</p>
        </>
      )}
      {state === "error" && <p className="error-text">{msg}</p>}
    </div>
  );
}

export default function LinkPage() {
  return (
    <Suspense>
      <LinkFlow />
    </Suspense>
  );
}
