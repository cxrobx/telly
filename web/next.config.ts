import type { NextConfig } from "next";

// Telly's API. Only /api/* is proxied: /mcp and /internal/* stay reachable on the Docker
// network alone, never through the public site (spec §9.1).
const API = process.env.TELLY_API_URL ?? "http://localhost:8940";

const nextConfig: NextConfig = {
  output: "standalone",
  images: { unoptimized: true },
  // Set when a dev server is reached through a tunnel. The hostname stays out of this public repo.
  allowedDevOrigins: process.env.TELLY_DEV_ORIGIN ? [process.env.TELLY_DEV_ORIGIN] : [],
  // On a phone the dev badge sits on top of the chat's input.
  devIndicators: process.env.TELLY_DEV_ORIGIN ? false : undefined,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};

export default nextConfig;
