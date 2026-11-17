"""Paths/labels shared by the server (manager.py) and the sidecar (addon.py)."""

# Inside the sidecar container:
CONFIG_DIR = "/run/egress"
CONFIG_PATH = "/run/egress/config.json"  # {"policy": {...}, "secrets": {...}}
CA_PATH = "/run/egress/mitmproxy-ca.pem"  # combined key+cert PEM mitmproxy loads from confdir
CONFIG_PATH_ENV = "EGRESS_CONFIG_PATH"  # addon reads the config path from this env (defaults to CONFIG_PATH)
# daiv-relay (relay/listen.go) sends this on each CONNECT it opens for the session's browser.
BROWSER_CLIENT_HEADER = "X-Daiv-Client"
BROWSER_CLIENT = "browser"
