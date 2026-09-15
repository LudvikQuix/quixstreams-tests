/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static HTML/JS into out/, served by the FastAPI process. No Node at runtime,
  // which is what keeps the deployment to one container and one process.
  output: 'export',

  // Every route becomes <route>/index.html, which is what StaticFiles expects.
  trailingSlash: true,

  // The Next.js image optimizer needs a Node server; there is none at runtime.
  images: { unoptimized: true },

  reactStrictMode: true,
}

module.exports = nextConfig
