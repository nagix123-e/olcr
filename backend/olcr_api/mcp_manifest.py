"""OLCR-owned MCP policy manifest.

Entries are deliberately disabled until an exact upstream release/commit has
been audited and packaged by the release workflow.  Keeping the policy in one
place prevents call sites from inventing commands or broadening tool access.
"""
from __future__ import annotations
from typing import Any

MCP_MANIFEST: dict[str, dict[str, Any]] = {
    "serena": {"id":"serena","display_name":"Serena","enabled_by_policy":False,"version":"1.7.0","integrity":"sha256:6dbf1459670d96fb0595f84932adef34260a6fe14ba5135b901fdb3c8c76e891","transport":"stdio","command":"bundled-python","args":["-c","from serena.cli import top_level; top_level()","start-mcp-server","--transport","stdio","--enable-web-dashboard","false","--enable-gui-log-window","false","--open-web-dashboard","false"],"working_directory_policy":"authorized_repo","allowed_network_hosts":[],"allowed_tools":["find_symbol","find_referencing_symbols","get_symbols_overview"],"startup_timeout":10.0,"tool_timeout":20.0,"shutdown_timeout":2.0,"fallback":"repo_tools","source":"https://github.com/oraios/serena","license":"MIT"},
    "devdocs": {"id":"devdocs","display_name":"DevDocs MCP","enabled_by_policy":False,"version":"COMMIT_REQUIRED","transport":"stdio","command":None,"args":[],"working_directory_policy":"authorized_repo","allowed_network_hosts":["devdocs.io","documents.devdocs.io"],"allowed_tools":["list_docs","search_docs","fetch_doc"],"startup_timeout":10.0,"tool_timeout":15.0,"shutdown_timeout":2.0,"fallback":"knowledge_repo","source":"https://github.com/elblanco2/devdocs-mcp-server","license":"MIT"},
    "playwright": {"id":"playwright","display_name":"Playwright MCP","enabled_by_policy":False,"version":"0.0.80","integrity":"sha512-FOPXHm2SvFhAQylm10jMZ35B/SR2TaMLVkavAlwoG4N2qCb5RqbvhQYcu3zmXNyxR2DW0Ooxe+9XPVt5UjKRCQ==","transport":"stdio","command":"bundled-node","args":["playwright"],"working_directory_policy":"frontend_dev","allowed_network_hosts":["127.0.0.1","localhost"],"allowed_tools":["browser_navigate","browser_click","browser_snapshot"],"startup_timeout":15.0,"tool_timeout":30.0,"shutdown_timeout":3.0,"fallback":"verification_not_run","source":"https://github.com/microsoft/playwright-mcp","license":"Apache-2.0"},
    "shadcn": {"id":"shadcn","display_name":"shadcn MCP","enabled_by_policy":True,"version":"4.16.2","integrity":"sha512-M1AvZKFWcCzWRDoyApIqJMSLIpY8Ev4uBGuiPLSFmiTbixXhPmzotSTvLzFmBrfoIxG9aIg2dZOETblEaXGUnQ==","transport":"stdio","command":"bundled-node","args":["shadcn"],"working_directory_policy":"authorized_repo","allowed_network_hosts":["ui.shadcn.com"],"allowed_tools":["search_items_in_registries","list_items_in_registries","view_items_in_registries"],"startup_timeout":10.0,"tool_timeout":20.0,"shutdown_timeout":2.0,"fallback":"existing_ui","source":"https://github.com/shadcn-ui/ui","license":"MIT"},
    "animejs": {"id":"animejs","display_name":"OLCR Anime.js v4 Reference MCP","enabled_by_policy":True,"version":"4.0.0","integrity":"olcr-owned-reference-corpus-v1","transport":"stdio","command":"bundled-node","args":["animejs"],"working_directory_policy":"none","allowed_network_hosts":[],"allowed_tools":["search_animejs_docs","get_animejs_api","get_animejs_example","get_animejs_pattern"],"startup_timeout":10.0,"tool_timeout":10.0,"shutdown_timeout":2.0,"fallback":"bundled_frontend_knowledge","source":"https://animejs.com/documentation/","license":"MIT"},
}

def server_definition(server_id: str) -> dict[str, Any] | None:
    value = MCP_MANIFEST.get(server_id)
    return dict(value) if value else None
