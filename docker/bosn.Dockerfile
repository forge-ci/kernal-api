FROM rust:1.95.0-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        clang \
        curl \
        git \
        libssl-dev \
        lld \
        libgtk-3-dev \
        libwebkit2gtk-4.1-dev \
        pkg-config \
        python3 \
        python3-venv \
        zstd \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/soldr \
    && /opt/soldr/bin/pip install --disable-pip-version-check --upgrade soldr

ENV PATH="/opt/soldr/bin:${PATH}"
# Cargo's test harness expects the login-session identity variables it has on CI.
ENV USER=root \
    HOME=/root
WORKDIR /repo
