"""Static landing page at ``/`` — the project's manifesto, typography-only.

A single self-contained HTML document (no external assets, no JS, no
fonts to fetch — just CSS with a system-font stack). Mirrors VISION.md
§1 verbatim so the page stays in sync with the constitution by hand:
when §1 changes, update _LANDING_HTML below in the same commit.

Why a manifesto-page and not a marketing site:
  - Pre-launch phase (Phase 0/1). Name, pricing, license still deferred.
  - A marketing site implies a product to onboard; we don't have that.
  - A pure manifesto sets positioning without committing to specifics.
  - When Phase 6 hits, this page either evolves or gets replaced — but
    nothing here will be a sunk cost.

The page is GET-only on ``/``. POST ``/`` still routes to the MCP
server (Starlette tries the GET route, partial-match fails on POST,
continues to the Mount which catches it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import HTMLResponse

if TYPE_CHECKING:
    from starlette.requests import Request


_LANDING_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>neverforget — vision</title>
  <meta name="description" content="A user-owned, vendor-neutral, self-organizing cognitive memory layer for AI agents.">
  <meta name="robots" content="noindex">
  <meta name="theme-color" content="#0a0a0a" media="(prefers-color-scheme: dark)">
  <meta name="theme-color" content="#fafaf7" media="(prefers-color-scheme: light)">
  <link rel="icon" href="data:,">
  <style>
    :root {
      --bg: #0a0a0a;
      --fg: #ebe9e6;
      --muted: #7a7771;
      --accent: #d9b86c;
      --rule: #1d1c1b;
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #fafaf7;
        --fg: #1a1918;
        --muted: #7c7975;
        --accent: #8a6b1f;
        --rule: #e6e3dd;
      }
    }
    *, *::before, *::after { box-sizing: border-box; }
    html { -webkit-text-size-adjust: 100%; text-rendering: optimizeLegibility; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      font-size: 17px;
      line-height: 1.65;
      font-feature-settings: "kern", "liga";
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }
    main {
      max-width: 38rem;
      margin: 0 auto;
      padding: 9rem 1.75rem 5rem;
    }
    .eyebrow {
      font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
      color: var(--muted);
      font-size: 0.78rem;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      margin: 0 0 4.5rem;
    }
    blockquote {
      margin: 0 0 3rem;
      padding: 0 0 0 1.5rem;
      border-left: 2px solid var(--accent);
      font-family: "Charter", "Iowan Old Style", "Palatino Linotype", "Palatino", "Georgia", "Times New Roman", serif;
      font-size: 1.7rem;
      line-height: 1.32;
      font-weight: 500;
      letter-spacing: -0.005em;
    }
    p {
      margin: 0 0 1.5rem;
      max-width: 36rem;
    }
    em { font-style: italic; color: var(--fg); }
    strong { font-weight: 600; color: var(--accent); letter-spacing: -0.005em; }
    hr {
      border: 0;
      border-top: 1px solid var(--rule);
      margin: 4.5rem 0 1.75rem;
    }
    footer {
      color: var(--muted);
      font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
      font-size: 0.74rem;
      letter-spacing: 0.06em;
    }
    footer a {
      color: var(--muted);
      text-decoration: none;
      border-bottom: 1px solid var(--rule);
      padding-bottom: 1px;
      transition: color 120ms ease, border-color 120ms ease;
    }
    footer a:hover, footer a:focus { color: var(--accent); border-color: var(--accent); outline: none; }
    @media (max-width: 480px) {
      main { padding: 4.5rem 1.25rem 3rem; }
      .eyebrow { margin-bottom: 3rem; }
      blockquote { font-size: 1.32rem; padding-left: 1rem; margin-bottom: 2.25rem; }
      p { font-size: 16.5px; }
      hr { margin: 3.5rem 0 1.5rem; }
    }
    @media (prefers-reduced-motion: reduce) {
      footer a { transition: none; }
    }
  </style>
</head>
<body>
  <main>
    <p class="eyebrow">Codename · neverforget</p>

    <blockquote>
      Every individual owns a digital extension of their mind &mdash;
      that travels with them, grows with them, and works for them
      across every AI system, every surface, and every vendor.
    </blockquote>

    <p>
      The inversion: today, Anthropic, OpenAI, Google, and Cursor own
      <em>your</em> context. They are the vaults; you rent space.
    </p>

    <p>
      This project flips the relationship. The user owns the substrate.
      The AI tools are clients.
    </p>

    <p>
      The end state is not another memory framework for developers.
      The end state is a <strong>cognitive sovereignty layer</strong>
      for the next decade &mdash; the way 1Password liberated
      credentials from individual browsers, this liberates context
      from individual AI silos.
    </p>

    <hr>

    <footer>
      Phase 1 &middot; in execution &middot; <a href="/health">status</a>
    </footer>
  </main>
</body>
</html>
"""


async def index(_request: Request) -> HTMLResponse:
    """Serve the static manifesto page. GET /; POST / still goes to MCP."""
    return HTMLResponse(
        _LANDING_HTML,
        # 5-min browser cache; content rarely changes, and changes happen
        # via redeploy which invalidates the in-memory string anyway.
        headers={"Cache-Control": "public, max-age=300, must-revalidate"},
    )
