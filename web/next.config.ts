import type { NextConfig } from "next";

// Telly's API. Only /api/* is proxied: /mcp and /internal/* stay reachable on the Docker
// network alone, never through the public site (spec §9.1).
const API = process.env.TELLY_API_URL ?? "http://localhost:8940";

const nextConfig: NextConfig = {
  output: "standalone",
  images: { unoptimized: true },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};

export default nextConfig;
