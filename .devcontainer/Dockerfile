FROM mcr.microsoft.com/devcontainers/python:3.11-bullseye

# ── System packages ────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    fish \
    lolcat \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# ── Fish as default shell for the vscode user ─────────────────────────────────
RUN chsh -s /usr/bin/fish vscode

# ── Python dev dependencies ───────────────────────────────────────────────────
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt

# ── Service Python dependencies ───────────────────────────────────────────────
COPY services/cw-decoder/requirements.txt /tmp/cw-requirements.txt
COPY services/sstv-decoder/requirements.txt /tmp/sstv-requirements.txt
COPY services/rtl-bridge/requirements.txt /tmp/rtl-requirements.txt
COPY services/psk31-decoder/requirements.txt /tmp/psk31-requirements.txt
COPY services/easypal-decoder/requirements.txt /tmp/easypal-requirements.txt
RUN pip install --no-cache-dir \
    -r /tmp/cw-requirements.txt \
    -r /tmp/sstv-requirements.txt \
    -r /tmp/rtl-requirements.txt \
    -r /tmp/psk31-requirements.txt \
    -r /tmp/easypal-requirements.txt
