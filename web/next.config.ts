import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  turbopack: { root: path.resolve(__dirname) },
  agentRules: false,
};
export default nextConfig;
