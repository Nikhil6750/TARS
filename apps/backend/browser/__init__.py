"""Real-browser control for TARS's BrowserAgent.

This package drives the user's actual Chrome via the Chrome DevTools Protocol
(CDP) -- tabs, navigation, DOM/accessibility-backed element resolution,
extraction -- as the execution method for arbitrary websites (YouTube,
Google, etc.).

It is deliberately separate from `skills/browser.py`, which controls TARS's
own embedded dashboard webview via `actions/frontend_bridge.py`. Same
"no vision, no blind coordinates" principle, different target surface:
`skills/browser.py` -> TARS's own UI; `skills/web_browser.py` (this
package's skill) -> any website in a real, inspectable browser tab.
"""
