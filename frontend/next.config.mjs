/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Self-contained build (minimal node_modules + server.js) for the Docker image.
  output: "standalone",
};

export default nextConfig;
