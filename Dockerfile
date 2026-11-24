FROM python:3.12-slim
# chromium = high-fidelity PDF export; libarchive-tools = RAR support via bsdtar
# Build with --build-arg WITH_OFFICE=1 to add LibreOffice for Word/Excel/PowerPoint attachment previews (+~600 MB)
ARG WITH_OFFICE=0
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium fonts-dejavu fonts-liberation fonts-noto-cjk libarchive-tools curl \
    && if [ "$WITH_OFFICE" = "1" ]; then apt-get install -y --no-install-recommends libreoffice-writer libreoffice-calc libreoffice-impress; fi \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mailvault ./mailvault
RUN useradd -u 1000 -m mailvault && mkdir -p /mail /tmp/mailvault /data && chown mailvault /tmp/mailvault /data
USER mailvault
# /data keeps search indexes between restarts (mount a volume there)
ENV MAILVAULT_ROOTS=/mail MAILVAULT_UPLOADS=/tmp/mailvault MAILVAULT_CACHE=/data/cache
EXPOSE 8080
HEALTHCHECK CMD curl -fs "http://127.0.0.1:${APP_PORT:-8080}/heartbeat" || exit 1
CMD ["python", "-m", "mailvault", "serve", "--host", "0.0.0.0", "--port", "8080", "--data", "/mail"]
