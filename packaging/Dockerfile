FROM docker.io/library/python:3.14.3-slim-bookworm@sha256:c6f0b5b3a167963de3cc7cd97fe1a5d07105c8d27f47507ab31d648b533b05c7 AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY app/Backend/requirements.production.lock /build/requirements.production.lock
RUN test "$(python -c 'import platform; print(platform.python_version())')" = 3.14.3 && \
    test "$(uname -m)" = x86_64 && \
    echo '1d8e368b5645db69cab33479c56d7db0827155898368a419fc80ab9819ee5736  /build/requirements.production.lock' | sha256sum -c - && \
    python -m venv /opt/venv && \
    /opt/venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps --no-compile --no-cache-dir -r /build/requirements.production.lock && \
    /opt/venv/bin/python -m pip check && \
    find /opt/venv -type f -name '*.pyc' -delete && \
    find /opt/venv -type d -name '__pycache__' -empty -delete

FROM docker.io/library/python:3.14.3-slim-bookworm@sha256:c6f0b5b3a167963de3cc7cd97fe1a5d07105c8d27f47507ab31d648b533b05c7
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
    FLOWSIGNAL_STATE_ROOT=/state FLOWSIGNAL_CACHE_ROOT=/cache \
    TMPDIR=/cache/tmp STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED=0
COPY --from=dependencies /opt/venv /opt/venv
COPY app/ /app/
COPY release/build-record.json /app/build-record.json
COPY release/expectations.json /opt/flowsignal-release/expectations.json
COPY start.sh /usr/local/bin/flowsignal-start
RUN /usr/sbin/groupadd --gid 1000 flowsignal && /usr/sbin/useradd --uid 501 --gid 1000 --no-create-home --home-dir /nonexistent flowsignal && \
    mkdir -p /state /cache/tmp && chown -R 501:1000 /state /cache && chmod 0700 /state /cache /cache/tmp && \
    chown -R 0:0 /app /opt/flowsignal-release /opt/venv /usr/local/bin/flowsignal-start && \
    find /app /opt/flowsignal-release /opt/venv -type d -exec chmod 0555 '{}' + && \
    find /app /opt/flowsignal-release /opt/venv -type f -exec chmod a-w '{}' + && \
    chmod 0444 /app/build-record.json /opt/flowsignal-release/expectations.json && chmod 0555 /usr/local/bin/flowsignal-start
WORKDIR /app/Backend
USER 501:1000
ENTRYPOINT ["/usr/local/bin/flowsignal-start"]
