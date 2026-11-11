# Moli's official release is x86_64 GNU/Linux (requires glibc >= 2.35).
# Debian bookworm supplies glibc 2.36. Both stages are amd64 intentionally.
FROM --platform=linux/amd64 node:22.21.1-bookworm-slim@sha256:25b3eb23a00590b7499f2a2ce939322727fcce1b15fdd69754fcd09536a3ae2c AS node
FROM --platform=linux/amd64 python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    SCOUT_MOLI_BIN=/usr/local/bin/moli \
    SCOUT_PLAYWRIGHT_MODULE=/app/node_modules/playwright
WORKDIR /app
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libstdc++6 libgcc-s1 zlib1g libfontconfig1 fonts-dejavu-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Verify the actual release archive before extracting or executing it.
RUN curl --fail --location --retry 3 --connect-timeout 20 \
      https://github.com/lexmount/moli/releases/download/v1.1.23/moli-x86_64-unknown-linux-gnu.tar.gz \
      --output /tmp/moli.tar.gz \
    && echo 'df360c04e567ce2e56a8391a03dc677d794465848b5821f944d2de10f3f1f430  /tmp/moli.tar.gz' | sha256sum --check --strict \
    && mkdir /tmp/moli-release \
    && tar -xzf /tmp/moli.tar.gz --strip-components=1 -C /tmp/moli-release \
    && install -m 0755 /tmp/moli-release/moli /usr/local/bin/moli \
    && mkdir -p /usr/local/share/licenses/moli \
    && cp -R /tmp/moli-release/LICENSE-* /tmp/moli-release/licenses /tmp/moli-release/license-metadata.json /usr/local/share/licenses/moli/ \
    && /usr/local/bin/moli --version \
    && rm -rf /tmp/moli-release /tmp/moli.tar.gz

COPY package.json package-lock.json requirements.txt ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home --uid 10001 scout \
    && chown -R scout:scout /app
USER scout
CMD ["python", "cloud_worker.py"]
