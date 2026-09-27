"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";

function SignIn() {
  const back = useSearchParams().get("back");
  const router = useRouter();
  const [state, setState] = useState<"idle" | "waiting" | "error">(back ? "waiting" : "idle");
  const [msg, setMsg] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    if (!back || started.current) return;
    started.current = true;
    (async () => {
      for (let i = 0; i < 60; i++) {
        try {
          const r = await api<{ done: boolean }>("/auth/finish", { method: "POST" });
          if (r.done) {
            router.replace("/");
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
      setMsg("Timed out waiting for Plex. Try again.");
    })();
  }, [back, router]);

  const start = async () => {
    try {
      const r = await api<{ auth_url: string }>("/auth/start", { method: "POST" });
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
      <p className="signin-lede">What to watch next, and what&apos;s coming for the shows you love.</p>
      {state === "waiting" ? (
        <p className="muted">Finishing sign-in with Plex…</p>
      ) : (
        <button className="btn btn-plex" onClick={start}>
          Sign in with Plex
        </button>
      )}
      {state === "error" && <p className="error-text">{msg}</p>}
      <p className="muted small">For members of Chris&apos;s Plex server.</p>
    </div>
  );
}

export default function SignInPage() {
  return (
    <Suspense>
      <SignIn />
    </Suspense>
  );
}
