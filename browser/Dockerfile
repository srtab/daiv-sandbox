#########################################################################################################
# daiv-relay: a static binary, so the same file runs in the browser image and in any sandbox base image
#########################################################################################################
FROM golang:1.25-trixie AS relay

WORKDIR /src
COPY relay/ ./
RUN go vet ./... && go test ./... \
  && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/daiv-relay .

#########################################################################################################
# Browser image: Debian's Chromium (never branded Chrome or chrome-headless-shell, which ignore
# --load-extension), driven by daiv-sandbox beside a session's sandbox
#########################################################################################################
FROM debian:trixie-slim

LABEL maintainer="srtabs@gmail.com"

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  chromium \
  libnss3-tools \
  python3 \
  ca-certificates \
  fonts-dejavu-core \
  fonts-liberation \
  fonts-noto-color-emoji \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* /var/cache/* \
  && groupadd --gid 1000 browser \
  && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/browser --shell /usr/sbin/nologin browser \
  && mkdir -p /profile /opt/daiv/extension /etc/chromium/policies/managed \
  && chown 1000:1000 /profile /opt/daiv/extension

COPY browser/policies/daiv.json /etc/chromium/policies/managed/daiv.json
COPY browser/launch.py browser/save_tabs.py /opt/daiv/
COPY --from=relay /out/daiv-relay /usr/local/bin/daiv-relay

ENV HOME=/home/browser
USER 1000:1000
WORKDIR /home/browser

CMD ["sleep", "infinity"]
