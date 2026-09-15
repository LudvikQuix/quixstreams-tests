import type { Metadata } from "next"

import "./globals.css"
import "react-grid-layout/css/styles.css"
import "react-resizable/css/styles.css"
import "uplot/dist/uPlot.min.css"

import { Toaster } from "@/components/ui/toaster"
import { ThemeContextProvider } from "@/lib/contexts/theme-context"

export const metadata: Metadata = {
  title: "SIL Dashboard",
  description: "Configurable real-time grid for software-in-the-loop plant simulation — charts, readouts and controls.",
  // SVG favicon with an embedded prefers-color-scheme media query handles both
  // light and dark browser chrome from a single file (see public/favicon.svg).
  // The dark-mode variant uses the media query inside the SVG rather than a
  // second <link media="..."> tag, because Next.js Metadata does not expose a
  // media attribute on icon entries. The SVG approach is equivalent and keeps
  // the metadata declaration to one icon entry.
  //
  // favicon-32.png and apple-touch-icon.png must be generated from favicon.svg
  // before a production build ships them. Generate with, e.g.:
  //   npx svgexport public/favicon.svg public/favicon-32.png 32:32
  //   npx svgexport public/favicon.svg public/apple-touch-icon.png 180:180
  // or any rasteriser (inkscape, rsvg-convert, sharp). Until they exist the
  // <link> tags are inert — browsers fall back gracefully to the SVG.
  icons: {
    icon: [
      { url: "/favicon.svg", type: "image/svg+xml" },
      { url: "/favicon-32.png", type: "image/png", sizes: "32x32" },
    ],
    apple: { url: "/apple-touch-icon.png", type: "image/png" },
  },
  manifest: "/site.webmanifest",
}

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>): JSX.Element {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-background text-foreground">
        <ThemeContextProvider>
          {children}
          <Toaster />
        </ThemeContextProvider>
      </body>
    </html>
  )
}
