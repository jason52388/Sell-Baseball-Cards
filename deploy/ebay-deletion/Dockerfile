# Tiny image for the eBay account-deletion responder (stdlib only, no deps).
FROM python:3.12-alpine
COPY responder.py /app/responder.py
WORKDIR /app
EXPOSE 8787
CMD ["python3", "/app/responder.py"]
