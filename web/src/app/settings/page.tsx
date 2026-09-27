"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { QRCodeSVG } from "qrcode.react";
import { Bell, LogOut, MessageCircle, Smartphone, Unplug } from "lucide-react";
import { useBackdrop } from "@/components/backdrop";
import { Chip, Section, Skeleton } from "@/components/ui";
import { TasteSection } from "@/components/taste";
import { api, useApi, type Me } from "@/lib/api";

type Member = {
  username: string;
  signed_in: boolean;
  watchlist_connected: boolean;
  discord_linked: boolean;
  phone_alerts: boolean;
  removed: boolean;
};

function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button role="switch" aria-checked={on} aria-label={label} className="toggle" onClick={() => onChange(!on)}>
      <span className="toggle-knob" />
    </button>
  );
}

function Members() {
  const { data } = useApi<{ members: Member[] }>("/members");
  if (!data) return null;
  const yes = (v: boolean) => (v ? <Chip tone="good">Yes</Chip> : <span className="muted">—</span>);
  return (
    <Section title="Members">
      <div className="glass table-wrap">
        <table className="members">
          <thead>
            <tr>
              <th>Member</th>
              <th>Signed in</th>
              <th>Watchlist</th>
              <th>Discord</th>
              <th>Phone</th>
            </tr>
          </thead>
          <tbody>
            {data.members
              .filter((m) => !m.removed)
              .map((m) => (
                <tr key={m.username || "unnamed"}>
                  <td>{m.username || <span className="muted">(managed user)</span>}</td>
                  <td>{yes(m.signed_in)}</td>
                  <td>{yes(m.watchlist_connected)}</td>
                  <td>{yes(m.discord_linked)}</td>
                  <td>{yes(m.phone_alerts)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">No one&apos;s viewing is shown here, including to you.</p>
    </Section>
  );
}

export default function SettingsPage() {
  const { data: me, loading, setData, reload } = useApi<Me>("/me");
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  useBackdrop(null);
  if (loading || !me) return <Skeleton rows={3} />;

  const save = async (body: { phone_alerts?: boolean; discord_dms?: boolean }) => {
    setSaving(true);
    await api("/settings", { body });
    reload();
    setSaving(false);
  };
  const ntfyLink = me.phone_topic ? `${me.ntfy_url}/${me.phone_topic}` : null;

  return (
    <div className="page narrow">
      <header className="page-head">
        <h1 className="page-title">Settings</h1>
        <p className="page-sub">Signed in as {me.username} · following {me.following} shows</p>
      </header>

      <Section title="Alerts">
        <div className="glass settings-card">
          <div className="setting">
            <MessageCircle size={20} />
            <div className="setting-text">
              <div className="setting-name">Discord DMs from plexbot</div>
              <div className="muted small">
                {me.discord_linked ? (
                  <>Linked to {me.discord_name ?? "your Discord"}.</>
                ) : (
                  <>
                    Not linked yet: type <code>!link</code> in #plexbot and open the link it DMs you.
                  </>
                )}
              </div>
            </div>
            <Toggle on={me.discord_dms && me.discord_linked} label="Discord DMs" onChange={(v) => me.discord_linked && save({ discord_dms: v })} />
          </div>
          <div className="setting">
            <Smartphone size={20} />
            <div className="setting-text">
              <div className="setting-name">Phone notifications</div>
              <div className="muted small">Lock-screen alerts through the free ntfy app. Your topic is private: don&apos;t share it.</div>
            </div>
            <Toggle on={!!me.phone_topic} label="Phone notifications" onChange={(v) => save({ phone_alerts: v })} />
          </div>
          {ntfyLink && (
            <div className="ntfy">
              <div className="qr">
                <QRCodeSVG value={ntfyLink} size={132} bgColor="transparent" fgColor="currentColor" />
              </div>
              <ol className="small ntfy-steps">
                <li>Install <strong>ntfy</strong> (App Store / Play Store).</li>
                <li>
                  Tap <strong>+</strong>, then subscribe to <code>{me.phone_topic}</code>
                </li>
                <li>Or open this on your phone: <a href={ntfyLink}>{ntfyLink}</a></li>
              </ol>
            </div>
          )}
          {saving && <div className="muted small">Saving…</div>}
        </div>
      </Section>

      <Section title="Your taste">
        <TasteSection />
      </Section>

      <Section title="Plex">
        <div className="glass settings-card">
          <div className="setting">
            <Bell size={20} />
            <div className="setting-text">
              <div className="setting-name">Watchlist</div>
              <div className="muted small">
                {me.watchlist_connected
                  ? "Connected. Shows on your Plex watchlist are followed automatically."
                  : "Not connected. Sign in again to reconnect it."}
              </div>
            </div>
            {me.watchlist_connected && (
              <button
                className="btn btn-small ghost"
                onClick={async () => {
                  await api("/plex/disconnect", { method: "POST" });
                  setData({ ...me, watchlist_connected: false });
                }}
              >
                <Unplug size={14} /> Disconnect
              </button>
            )}
          </div>
          <div className="setting">
            <LogOut size={20} />
            <div className="setting-text">
              <div className="setting-name">Sign out</div>
            </div>
            <button
              className="btn btn-small ghost"
              onClick={async () => {
                await api("/auth/logout", { method: "POST" });
                router.replace("/signin");
              }}
            >
              Sign out
            </button>
          </div>
        </div>
      </Section>

      {me.is_owner && <Members />}
    </div>
  );
}
