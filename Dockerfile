FROM python:3.12-slim

# Chromium powers the "Render JavaScript" option
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY sitegrab.py sitegrab_ui.py ./

ENV SITEGRAB_HOSTED=1 \
    SITEGRAB_CHROME_NO_SANDBOX=1

EXPOSE 8737
CMD ["python3", "sitegrab_ui.py", "--no-open"]
