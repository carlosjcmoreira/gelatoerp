---
name: Playwright browser
description: Replit environment behavior when Playwright is present without its default cached browser.
---

When Playwright is installed but its expected browser cache is empty, the managed Chromium executable may still be available at `/repl/tools/bin/chromium`; pass it as `executablePath` before attempting a browser download.

**Why:** The system browser was available even though Playwright's default launch reported that its cached Chromium was missing.

**How to apply:** Use this path for local, temporary browser verification in this Replit environment. Keep browser binaries and test artifacts outside the project unless the user asks to retain them.