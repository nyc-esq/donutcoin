# A Donut Coin node in a container: the chain, the API, the explorer and the browser wallet.
#
# Everything that must survive a rebuild lives in /data, which is why both DONUTCOIN_DATA and
# DONUTCOIN_WALLET are set: the wallet path does not follow the data directory, and a wallet
# written outside the volume would be destroyed by the next `docker compose up --build`. That
# file is the only thing here that cannot be re-downloaded.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Non-root. The named volume in compose is chowned on creation; a bind mount needs the host
# directory to be writable by uid 1000, or you will get a permission error on first write.
RUN useradd --uid 1000 --create-home donut && mkdir -p /data && chown donut:donut /data
USER donut

ENV DONUTCOIN_DATA=/data \
    DONUTCOIN_WALLET=/data/wallet.json \
    DONUTCOIN_PORT=8555 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8555

# Liveness only, deliberately: "is it serving?" and not "is it caught up?". A node that is still
# doing its first sync is legitimately behind, and a healthcheck that failed on chain height would
# restart it forever, exactly when it most needs to be left alone. Use /api/info yourself to see
# how far along it is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8555/api/info', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "donutcoin"]
CMD ["node"]
