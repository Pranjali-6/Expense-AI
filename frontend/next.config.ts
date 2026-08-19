import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Required by the production Docker target.
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,

  eslint: { ignoreDuringBuilds: true },

  experimental: {
    // Trim the client bundle for icon and chart imports.
    optimizePackageImports: ["lucide-react", "recharts", "date-fns"],
  },

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },

  async rewrites() {
    // In development the browser talks to Next directly on :3000, so proxy the
    // API through. Behind nginx this rewrite is never exercised.
    return [
      {
        source: "/api/v1/:path*",
        destination: `${process.env.API_INTERNAL_URL ?? "http://api:8000"}/api/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
