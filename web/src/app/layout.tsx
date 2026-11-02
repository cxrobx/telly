import type { Metadata, Viewport } from "next";
import { Geist, Poppins } from "next/font/google";
import { Shell } from "@/components/shell";
import "./globals.css";

const sans = Geist({ variable: "--font-sans", subsets: ["latin"] });
const display = Poppins({ variable: "--font-display", weight: ["600", "700"], subsets: ["latin"] });

const description = "What to watch next, and what's coming for the shows you love.";

// og:image (app/opengraph-image.jpg) must be an absolute URL, or iMessage shows no card.
export const metadata: Metadata = {
  metadataBase: new URL("https://telly.chrisx.art"),
  title: "Telly",
  description,
  openGraph: { type: "website", siteName: "Telly", title: "Telly", description },
};

// Zoom is locked. Without this, a phone zooms the page in on a tap or a field taking focus and
// leaves it there. Safari ignores userScalable, so globals.css holds the other half: fields at
// 16px (its real trigger for the focus zoom) and touch-action against the double-tap zoom.
export const viewport: Viewport = {
  themeColor: "#07070b",
  colorScheme: "dark",
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  // A Home Screen install fills the screen either way, but only cover makes env(safe-area-inset-*)
  // report anything. Without it the insets read 0 and the tab bar sits on the home indicator.
  viewportFit: "cover",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${sans.variable} ${display.variable}`}>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
