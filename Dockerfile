FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    libimage-exiftool-perl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY fill_missing_exif.py /app/fill_missing_exif.py

ENTRYPOINT ["python", "/app/fill_missing_exif.py"]
CMD ["--help"]
