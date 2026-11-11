# Moli's official release is x86_64 GNU/Linux (requires glibc >= 2.35).
# Debian bookworm supplies glibc 2.36. Both stages are amd64 intentionally.
FROM --platform=linux/amd64 node:22.21.1-bookworm-slim@sha256:25b3eb23a00590b7499f2a2ce939322727fcce1b15fdd69754fcd09536a3ae2c AS node
FROM --platform=linux/amd64 python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SCOUT_MOLI_BIN=/usr/local/bin/moli \
    SCOUT_CHROME_BIN=/opt/chrome-headless-shell/chrome-headless-shell \
    SCOUT_PLAYWRIGHT_MODULE=/app/node_modules/playwright
WORKDIR /app
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libstdc++6 libgcc-s1 zlib1g libfontconfig1 fonts-dejavu-core fonts-noto-cjk \
       libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 \
       libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 unzip \
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

# Chrome fallback: the headless shell Playwright 1.65.0 pins (chromium-headless-shell revision 1254 = 158.0.8127.18).
# Downloaded and hash-checked here because `playwright install` only checks Content-Length.
# sha256 verified 2026-11-12: identical file (131998580 bytes) from this URL and from
# https://storage.googleapis.com/chrome-for-testing-public/158.0.8127.18/linux64/chrome-headless-shell-linux64.zip.
ARG CHROME_HEADLESS_SHELL_VERSION=158.0.8127.18
ARG CHROME_HEADLESS_SHELL_SHA256=18352d22cb0cdf520cbff2ad8c41391f0e02a72886cb53b0fde4bdfc4bd075d6
RUN curl --fail --location --retry 3 --connect-timeout 20 \
      https://cdn.playwright.dev/builds/cft/${CHROME_HEADLESS_SHELL_VERSION}/linux64/chrome-headless-shell-linux64.zip \
      --output /tmp/chrome-headless-shell.zip \
    && echo "${CHROME_HEADLESS_SHELL_SHA256}  /tmp/chrome-headless-shell.zip" | sha256sum --check --strict \
    && unzip -q /tmp/chrome-headless-shell.zip -d /tmp/chrome-release \
    && mv /tmp/chrome-release/chrome-headless-shell-linux64 /opt/chrome-headless-shell \
    && rm -rf /tmp/chrome-release /tmp/chrome-headless-shell.zip \
    && apt-get purge -y --auto-remove unzip \
    && if ldd /opt/chrome-headless-shell/chrome-headless-shell | grep 'not found'; then echo 'Chrome 缺少系统库' >&2; exit 1; fi \
    && /opt/chrome-headless-shell/chrome-headless-shell --version

COPY package.json package-lock.json requirements.txt ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && pip install --no-cache-dir -r requirements.txt \
    && CHROME_HEADLESS_SHELL_VERSION=${CHROME_HEADLESS_SHELL_VERSION} node -e "const b=require('/app/node_modules/playwright-core/browsers.json').browsers.find(x=>x.name==='chromium-headless-shell');if(!b||b.browserVersion!==process.env.CHROME_HEADLESS_SHELL_VERSION){console.error('Playwright 期望 headless shell '+(b&&b.browserVersion)+'，镜像安装的是 '+process.env.CHROME_HEADLESS_SHELL_VERSION+'，请同步升级');process.exit(1)}"
COPY . .
RUN useradd --create-home --uid 10001 scout \
    && chown -R scout:scout /app
USER scout
CMD ["python", "cloud_worker.py"]
