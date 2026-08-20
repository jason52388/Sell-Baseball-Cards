# Tiny image for the eBay account-deletion responder (stdlib only, no deps).
FROM python:3.12-alpine
COPY responder.py /app/responder.py
WORKDIR /app
EXPOSE 8787
# The responder needs no write access and no privileges; the systemd variant of
# this same service is hardened the same way.
USER nobody
CMD ["python3", "/app/responder.py"]
