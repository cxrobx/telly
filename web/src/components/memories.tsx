"use client";

import { useState } from "react";
import { Check, Pencil, Trash2, X } from "lucide-react";
import { Chip, Empty, Skeleton } from "@/components/ui";
import { api, ApiError, useApi } from "@/lib/api";

// What Telly remembers about you (docs/spec-memory.md): lasting things you told plexbot or
// typed here. Only ever yours; plexbot reads them at the start of every chat turn.
type Kind = "preference" | "setup" | "plan";
type Memory = { id: number; kind: Kind; text: string; source: "chat" | "told" | "web"; updated_at: string; expires?: string };
type Data = { memories: Memory[]; max: number; max_len: number };

const KINDS: { kind: Kind; label: string; example: string }[] = [
  { kind: "preference", label: "Preference", example: "Subtitles, not dubs, for anime" },
  { kind: "setup", label: "Setup", example: "The bedroom TV can't play 4K" },
  { kind: "plan", label: "Plan", example: "Watching Bleach canon-only, on episode 40" },
];
const SOURCE = { chat: "saved in chat", told: "you asked plexbot", web: "added here" } as const;

function message(e: unknown) {
  return e instanceof ApiError ? e.message : "Couldn't save that. Try again.";
}

function MemoryForm({
  initial,
  maxLen,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial?: { text: string; kind: Kind };
  maxLen: number;
  submitLabel: string;
  onSubmit: (text: string, kind: Kind) => Promise<void>;
  onCancel?: () => void;
}) {
  const [text, setText] = useState(initial?.text ?? "");
  const [kind, setKind] = useState<Kind>(initial?.kind ?? "preference");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(text.trim(), kind);
      if (!initial) setText("");
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="memory-form" onSubmit={submit}>
      <div className="memory-kinds" role="radiogroup" aria-label="Kind">
        {KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            role="radio"
            aria-checked={kind === k.kind}
            className="btn btn-small ghost memory-kind"
            data-on={kind === k.kind}
            onClick={() => setKind(k.kind)}
          >
            {k.label}
          </button>
        ))}
      </div>
      <div className="memory-input-row">
        <input
          className="memory-input"
          value={text}
          maxLength={maxLen}
          onChange={(e) => setText(e.target.value)}
          placeholder={KINDS.find((k) => k.kind === kind)!.example}
          aria-label="What Telly should remember"
          autoFocus={!!initial}
        />
        <button className="btn btn-small btn-primary" disabled={busy || !text.trim()}>
          {initial ? <Check size={14} /> : null} {submitLabel}
        </button>
        {onCancel && (
          <button type="button" className="btn btn-small ghost" onClick={onCancel} aria-label="Cancel">
            <X size={14} />
          </button>
        )}
      </div>
      {error && <p className="error-text">{error}</p>}
    </form>
  );
}

export function Memories() {
  const { data, loading, setData, reload } = useApi<Data>("/memories");
  const [editing, setEditing] = useState<number | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  const add = async (text: string, kind: Kind) => {
    const r = await api<{ memory: Memory }>("/memories", { body: { text, kind } });
    setData((d) => (d ? { ...d, memories: [...d.memories, r.memory] } : d));
  };
  const edit = (id: number) => async (text: string, kind: Kind) => {
    const r = await api<{ memory: Memory }>(`/memories/${id}/edit`, { body: { text, kind } });
    setData((d) => (d ? { ...d, memories: d.memories.map((m) => (m.id === id ? r.memory : m)) } : d));
    setEditing(null);
  };
  const remove = async (id: number) => {
    setData((d) => (d ? { ...d, memories: d.memories.filter((m) => m.id !== id) } : d));
    await api(`/memories/${id}/delete`, { method: "POST" }).catch(reload);
  };
  const clear = async () => {
    setConfirmClear(false);
    setData((d) => (d ? { ...d, memories: [] } : d));
    await api("/memories/clear", { method: "POST" }).catch(reload);
  };

  if (loading || !data) return <Skeleton rows={2} />;
  const full = data.memories.length >= data.max;
  return (
    <div className="glass memory-card">
      <p className="muted small">
        Lasting things you&apos;ve told plexbot about yourself, like subtitles over dubs, your TV, or where you are in a show. It uses them in every chat, on
        Discord and here, and says so when it saves one. Only you can see them. Plans are dropped after 60 days unless they&apos;re updated.
      </p>
      {data.memories.length === 0 ? (
        <Empty>Nothing yet. Tell plexbot something about how you watch, or add it below.</Empty>
      ) : (
        <ul className="memory-list" aria-label="What Telly remembers">
          {data.memories.map((m) =>
            editing === m.id ? (
              <li key={m.id} className="memory-row editing">
                <MemoryForm initial={m} maxLen={data.max_len} submitLabel="Save" onSubmit={edit(m.id)} onCancel={() => setEditing(null)} />
              </li>
            ) : (
              <li key={m.id} className="memory-row">
                <Chip tone={m.kind === "plan" ? "accent" : "neutral"}>{KINDS.find((k) => k.kind === m.kind)?.label ?? m.kind}</Chip>
                <span className="memory-text">
                  {m.text}
                  <span className="muted small memory-meta">
                    {SOURCE[m.source]}, {m.updated_at}
                    {m.expires ? ` · until ${m.expires}` : ""}
                  </span>
                </span>
                <span className="memory-actions">
                  <button className="icon-btn ghost" aria-label={`Edit: ${m.text}`} onClick={() => setEditing(m.id)}>
                    <Pencil size={15} />
                  </button>
                  <button className="icon-btn ghost" aria-label={`Forget: ${m.text}`} onClick={() => remove(m.id)}>
                    <Trash2 size={15} />
                  </button>
                </span>
              </li>
            ),
          )}
        </ul>
      )}
      {full ? (
        <p className="muted small">That&apos;s {data.max}, the most Telly keeps. Edit or forget one to add another.</p>
      ) : (
        <MemoryForm maxLen={data.max_len} submitLabel="Remember" onSubmit={add} />
      )}
      {data.memories.length > 0 &&
        (confirmClear ? (
          <div className="memory-clear">
            <span className="small">Forget all {data.memories.length}?</span>
            <button className="btn btn-small" onClick={clear}>
              Forget all
            </button>
            <button className="btn btn-small ghost" onClick={() => setConfirmClear(false)}>
              Keep them
            </button>
          </div>
        ) : (
          <button className="btn btn-small ghost memory-clear-btn" onClick={() => setConfirmClear(true)}>
            Clear all
          </button>
        ))}
    </div>
  );
}
