FROM apache/tika:latest-full

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr-eng \
        tesseract-ocr-ell \
    && rm -rf /var/lib/apt/lists/*
