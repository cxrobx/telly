import type { Metadata, Viewport } from "next";
import { Geist, Poppins } from "next/font/google";
import { Shell } from "@/components/shell";
import "./globals.css";

const sans = Geist({ variable: "--font-sans", subsets: ["latin"] });
const display = Poppins({ variable: "--font-display", weight: ["600", "700"], subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Telly",
  description: "What to watch next, and what's coming for the shows you love.",
};

export const viewport: Viewport = { themeColor: "#07070b", colorScheme: "dark" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${sans.variable} ${display.variable}`}>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
