"""One background process that owns the reader browser for every MCP session.

Each Claude session starts its own ``teams-reader mcp``; without a broker each
one would open (or fight over) its own Chromium on the single browser profile.
The broker holds that profile, runs the background sync, and serves browser
jobs over a Unix socket; MCP servers read the store directly and only ask the
broker for work that needs Teams web.
"""
