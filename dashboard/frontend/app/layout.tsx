import type { Metadata } from "next"

import "./globals.css"
import "react-grid-layout/css/styles.css"
import "react-resizable/css/styles.css"
import "uplot/dist/uPlot.min.css"

import { Toaster } from "@/components/ui/toaster"
import { ThemeContextProvider } from "@/lib/contexts/theme-context"

export const metadata: Metadata = {
  title: "SIL Dashboard",
  description: "Configurable grid UI for software-in-the-loop plant models",
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
