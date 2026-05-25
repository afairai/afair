"""Static landing page at ``/`` — the project's manifesto, typography-only.

A single self-contained HTML document (no external assets, no JS, no
fonts to fetch — just CSS with a system-font stack). Three stacked
sections — manifesto, foundations, architecture — that mirror
VISION.md §1, §2, and §6 respectively. When those sections change,
update the inline HTML in the same commit so the landing stays in sync
with the constitution by hand.

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
    section { margin-top: 6rem; }
    section .eyebrow { margin-bottom: 2rem; }
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

    /* foundations (§2) — numbered principles list */
    .principles {
      list-style: none;
      padding: 0;
      margin: 0;
    }
    .principles li {
      display: grid;
      grid-template-columns: 2.75rem 1fr;
      column-gap: 0.75rem;
      align-items: baseline;
      padding: 1.25rem 0;
      border-bottom: 1px solid var(--rule);
    }
    .principles li:first-child { border-top: 1px solid var(--rule); }
    .principles .num {
      font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
      font-size: 0.78rem;
      color: var(--muted);
      letter-spacing: 0.12em;
    }
    .principles .title {
      display: block;
      color: var(--fg);
      font-weight: 600;
      letter-spacing: -0.005em;
      margin-bottom: 0.25rem;
    }
    .principles .detail {
      display: block;
      color: var(--muted);
      font-size: 0.95rem;
      line-height: 1.55;
    }

    /* architecture (§6) — three-layer text spread */
    .layers {
      margin: 0;
      padding: 0;
      border-top: 1px solid var(--rule);
    }
    .layers > div {
      display: grid;
      grid-template-columns: 1fr auto;
      column-gap: 1.5rem;
      align-items: baseline;
      padding: 1.25rem 0;
      border-bottom: 1px solid var(--rule);
    }
    .layers dt {
      font-weight: 600;
      letter-spacing: -0.005em;
    }
    .layers dd {
      margin: 0;
      font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
      font-size: 0.78rem;
      color: var(--muted);
      letter-spacing: 0.04em;
      text-align: right;
    }
    .layers dd .i {
      color: var(--accent);
      letter-spacing: 0.08em;
    }

    hr {
      border: 0;
      border-top: 1px solid var(--rule);
      margin: 5rem 0 1.75rem;
    }
    footer {
      color: var(--muted);
      font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
      font-size: 0.74rem;
      letter-spacing: 0.06em;
    }
    @media (max-width: 480px) {
      main { padding: 4.5rem 1.25rem 3rem; }
      .eyebrow { margin-bottom: 3rem; }
      section { margin-top: 4.25rem; }
      section .eyebrow { margin-bottom: 1.5rem; }
      blockquote { font-size: 1.32rem; padding-left: 1rem; margin-bottom: 2.25rem; }
      p { font-size: 16.5px; }
      .principles li {
        grid-template-columns: 2rem 1fr;
        padding: 1rem 0;
      }
      .layers > div {
        grid-template-columns: 1fr;
        row-gap: 0.35rem;
        padding: 1rem 0;
      }
      .layers dd { text-align: left; }
      hr { margin: 3.5rem 0 1.5rem; }
    }
    @media (prefers-reduced-motion: reduce) {
      * { transition: none !important; }
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
      for the next decade &mdash; the way password managers liberated
      credentials from individual browsers, this liberates context
      from individual AI silos.
    </p>

    <section aria-labelledby="foundations">
      <p class="eyebrow" id="foundations">Foundations &middot; non-negotiable</p>
      <ol class="principles">
        <li>
          <span class="num">01</span>
          <div>
            <span class="title">The user owns the substrate.</span>
            <span class="detail">Self-hosting is first-class, not a fallback. Hosted is convenience, never structural dependency.</span>
          </div>
        </li>
        <li>
          <span class="num">02</span>
          <div>
            <span class="title">Single-tenant, always.</span>
            <span class="detail">Every instance belongs to exactly one user. Multi-tenancy is forbidden architecturally, not just practically.</span>
          </div>
        </li>
        <li>
          <span class="num">03</span>
          <div>
            <span class="title">Cross-vendor by default.</span>
            <span class="detail">Claude, GPT, Gemini, Mistral, local models &mdash; all equal citizens. If it only works with one provider, it has failed.</span>
          </div>
        </li>
        <li>
          <span class="num">04</span>
          <div>
            <span class="title">Schema is emergent, never imposed.</span>
            <span class="detail">A minimal bootstrap scaffold &mdash; categories grow from your interaction, not from a fixed taxonomy.</span>
          </div>
        </li>
      </ol>
    </section>

    <section aria-labelledby="architecture">
      <p class="eyebrow" id="architecture">Architecture &middot; three layers</p>
      <dl class="layers">
        <div>
          <dt>MCP Surface</dt>
          <dd>stable forever &middot; additive only &middot; <span class="i">I1</span></dd>
        </div>
        <div>
          <dt>Society of Mind</dt>
          <dd>salience &rarr; CEN &cup; DMN &middot; <span class="i">I3</span></dd>
        </div>
        <div>
          <dt>Substrate</dt>
          <dd>append-only &middot; content-addressed &middot; <span class="i">I2</span> &middot; on your disk</dd>
        </div>
      </dl>
    </section>

    <hr>

    <footer>
      Phase 1 &middot; in execution
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
