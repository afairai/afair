<claude-mem-context>
# Memory Context

# [neverforget] recent context, 2026-05-25 6:29pm GMT+2

Legend: 🎯session 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision 🚨security_alert 🔐security_note
Format: ID TIME TYPE TITLE
Fetch details: get_observations([IDs]) | Search: mem-search skill

Stats: 50 obs (18,315t read) | 316,202t work | 94% savings

### May 25, 2026
2197 4:41p 🔵 Fly Volume Durability Model for neverforget Phase 0
2198 4:51p ⚖️ Fly Deployment: GitHub Actions + Blue/Green Strategy Confirmed
2199 " 🔵 Blue/Green Deployment Incompatible with Single-Volume SQLite Architecture
2200 4:52p ⚖️ Fly Machine Lifecycle: Single Machine, Kill Old on Deploy
2201 5:03p 🟣 fly.toml Written: Single-Machine, Immediate Strategy, Always-On
2202 " ⚖️ Dockerfile Runs as Root — Documented Deviation from Non-Root Global Rule
2203 " 🟣 docs/operations.md Created: Full Fly Operations Runbook
2204 " 🟣 GitHub Actions Deploy Workflow Created
2205 5:04p 🔵 fly CLI Authenticated Locally; No FLY_API_TOKEN in Environment
2206 " 🔵 fly CLI Session Expired — Re-authentication Required
2207 5:08p ⚖️ Deploy Strategy Formally Accepted: strategy="immediate" for Phase 0
2208 5:23p 🔵 Claude Code MCP Server Auth Opens Browser for OAuth Login
2209 5:24p 🟣 neverforget: Fly.io Deploy + GitHub Actions CI Pipeline Shipped
2210 " 🔵 Clerk OAuth with Claude.ai MCP Has Known Bug; MCP Spec Requires OAuth 2.1
2211 5:30p 🔵 Clerk Cannot Act as OAuth Authorization Server for MCP Servers
2212 " 🔵 Clerk Has MCP OAuth Support via mcp-demo and mcp-tools Repos — Contradicts Docs
2213 " 🔵 clerk/mcp-tools Is Node.js Only — No Python Support for neverforget
2214 " 🔵 Claude.ai Clerk OAuth MCP Issue #164 Closed — Resolution Unknown, Claude Code CLI Works
2215 " ⚖️ Bearer Token Chosen as neverforget Phase 0 Auth Strategy
2216 5:31p 🔵 FastMCP Middleware API Shape for Bearer Token Auth Implementation
2217 5:37p 🟣 Bearer Token Auth Field Added to neverforget Settings
2218 5:38p 🟣 Production Boot Guard: Settings Fails if ENVIRONMENT=fly and AUTH_TOKEN Unset
2219 " 🟣 BearerTokenMiddleware Created as ASGI Middleware for neverforget MCP Server
2220 " 🟣 neverforget Server Refactored: ASGI Starlette Wrapper with Auth Middleware Added
2221 " 🟣 Comprehensive Auth Test Suite Created for neverforget Bearer Token Middleware
2222 5:39p ✅ NEVERFORGET_AUTH_TOKEN Added to .env.example with Generation Instructions
2223 " 🔵 FastMCP ASGI Integration Requires lifespan=mcp_app.lifespan in Parent Starlette App
2224 " 🔴 Fixed FastMCP ASGI Integration: lifespan=mcp_app.lifespan Added to Starlette Wrapper
2225 5:40p 🟣 All Auth Tests Green — Bearer Token Implementation Complete and Verified
2226 " 🟣 NEVERFORGET_AUTH_TOKEN Generated and Staged in Fly Secrets + .env.local
2227 " 🚨 NEVERFORGET_AUTH_TOKEN Marked Compromised — Leaked via .env.local Diff in Chat Transcript
2228 " 🔵 CI Deploy Failure Root Cause: Fly Remote Builder Cannot Find Dockerfile in GitHub Actions
2229 " 🟣 neverforget Redeployed to Production with Bearer Token Auth Active
2230 5:42p 🔵 Production /health Returns 503 After Auth Middleware Deploy — Database Check Failing
2231 " 🔵 Production Boot Guard Working — Server Crash-Looping Because Staged Secret Not Activated
S607 User asked how to automate the manual MCP connection setup process for the neverforget project (May 25 at 5:58 PM)
S608 User asked how to automate MCP client setup — full one-command installer built, tested, and shipped to main (May 25 at 5:58 PM)
2232 6:05p 🟣 Automated MCP client installer script created
2233 6:12p 🔴 install_clients.py crashed on empty JSON config files
2234 " ✅ docs/clients/README.md updated with one-command installer section
S609 User asked if neverforget MCP is already wired into the current Claude Code session — it is not yet (May 25 at 6:13 PM)
S611 Diagnosing why neverforget MCP isn't appearing in session despite installer success — found two-file Claude config split (May 25 at 6:14 PM)
2235 6:15p 🔵 All MCP clients were already configured before installer ran
S612 Fixed installer to write to ~/.claude.json — neverforget now in active Claude Code config, ready for session restart (May 25 at 6:15 PM)
S610 Installer run confirmed all MCP clients already configured — session restart is the only remaining step (May 25 at 6:15 PM)
2236 6:16p 🔵 Claude Code reads MCP config from ~/.claude.json, not ~/.claude/settings.json
2237 " 🔴 install_clients.py fixed to write to ~/.claude.json (the actual Claude Code config)
S613 Phase 0 capability gate achieved — first live neverforget remember call from Claude Code succeeded end-to-end (May 25 at 6:17 PM)
2238 6:17p 🔵 neverforget MCP tools now available in Claude Code session
2239 " 🟣 Phase 0 capability gate achieved — first live neverforget remember call from Claude Code
S614 Task #6 marked completed — Phase 0 cross-vendor MCP verification done, 6 of 7 tasks complete (May 25 at 6:17 PM)
2240 6:18p 🟣 Full remember→recall round-trip verified end-to-end in Claude Code
S615 User opened the Phase 0 journal and attempted Claude.ai connector — blocked by OAuth requirement (May 25 at 6:19 PM)
2241 6:22p ✅ Task #7 started — Phase 0 two-week daily-use journal
2242 6:23p 🟣 Phase 0 daily-use journal created with Day 1 entry
2243 6:26p 🔵 neverforget MCP Server Returns No Resources or Templates
2244 6:27p 🔵 Codex CLI uses http_headers not headers — neverforget entry may be missing auth
2245 " 🔵 Codex neverforget TOML block uses headers not http_headers — format mismatch confirmed
S616 Inspecting Codex CLI config to verify neverforget MCP entry and auth format (May 25 at 6:27 PM)
2246 6:29p 🔵 Codex MCP Server Configuration

Access 316k tokens of past work via get_observations([IDs]) or mem-search skill.
</claude-mem-context>