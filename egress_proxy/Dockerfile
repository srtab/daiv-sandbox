# egress_proxy/Dockerfile
# Sidecar egress proxy: mitmproxy + the daiv-sandbox egress addon.
FROM mitmproxy/mitmproxy:12.1.2

# Flat copy so the addon's fallback `import policy` / `import constants` resolves (mitmdump adds the
# script's directory to sys.path). policy.py/constants.py are stdlib-only; addon.py imports mitmproxy
# lazily, so all three import cleanly here.
COPY daiv_sandbox/egress/policy.py /addon/policy.py
COPY daiv_sandbox/egress/constants.py /addon/constants.py
COPY daiv_sandbox/egress/addon.py /addon/addon.py

# Owned by the run user (uid 1000 = the image's mitmproxy user and daiv-sandbox's RUN_UID default) so
# the non-root proxy can read its provisioned config/CA and write the CA files mitmproxy derives at boot.
RUN mkdir -p /run/egress && chown 1000:1000 /run/egress

# `--mode regular` = forward proxy; confdir holds mitmproxy-ca.pem (provisioned by the server before
# start). --set connection_strategy=lazy so unintercepted (passthrough) flows aren't eagerly opened.
CMD ["mitmdump", "--mode", "regular", "--listen-host", "0.0.0.0", "--listen-port", "8080", \
     "--set", "confdir=/run/egress", "--set", "connection_strategy=lazy", "-s", "/addon/addon.py"]
