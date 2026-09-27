"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

// One fixed, blurred backdrop behind every page: the artwork of whatever the page is about.
// Static on purpose (no animated blur): backdrop-filter is expensive and Chris's 5K runs hot.
const Ctx = createContext<(url: string | null) => void>(() => {});

export function BackdropProvider({ children }: { children: React.ReactNode }) {
  const [shown, setShown] = useState<{ a: string | null; b: string | null; front: "a" | "b" }>({
    a: null,
    b: null,
    front: "a",
  });
  // crossfade: put the new image in the back layer, then flip which layer is in front
  const setUrl = useCallback(
    (url: string | null) =>
      setShown((s) => {
        const current = s.front === "a" ? s.a : s.b;
        if (url === current) return s;
        return s.front === "a" ? { a: s.a, b: url, front: "b" } : { a: url, b: s.b, front: "a" };
      }),
    [],
  );
  return (
    <Ctx.Provider value={setUrl}>
      <div aria-hidden className="backdrop-root">
        {(["a", "b"] as const).map((k) => (
          <div
            key={k}
            className="backdrop-layer"
            data-front={shown.front === k}
            style={shown[k] ? { backgroundImage: `url(${shown[k]})` } : undefined}
          />
        ))}
        <div className="backdrop-shade" />
      </div>
      {children}
    </Ctx.Provider>
  );
}

export function useBackdrop(url: string | null | undefined) {
  const set = useContext(Ctx);
  useEffect(() => {
    if (url) set(url);
  }, [url, set]);
}
