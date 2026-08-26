FROM quay.io/fedora/fedora:latest

RUN dnf install -y \
  ca-certificates \
  fedora-messaging \
  git-core \
  public-inbox \
  public-inbox-server \
  && dnf clean all \
  && command -v fedora-messaging \
  && command -v public-inbox-init \
  && command -v public-inbox-httpd \
  && command -v public-inbox-mda \
  && command -v git \
  && test -f /etc/fedora-messaging/cacert.pem \
  && test -f /etc/fedora-messaging/fedora-key.pem \
  && test -f /etc/fedora-messaging/fedora-cert.pem

RUN mkdir -p /var/lib/public-inbox \
  && chgrp -R 0 /var/lib/public-inbox \
  && chmod -R g=u /var/lib/public-inbox

COPY consumer.py /usr/local/lib/consumer.py
COPY backfill.py /usr/local/bin/backfill.py

ENV FEDORA_MESSAGING_CONF=/etc/fedora-messaging/config.toml
ENV PYTHONPATH=/usr/local/lib

EXPOSE 8080 8081

USER 1001

CMD ["fedora-messaging", "consume"]
