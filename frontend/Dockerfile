# Sextant frontend — tina4-nodejs, server-rendered, no build step.
#
# Two constraints, both of which produce an image that fails at RUNTIME rather
# than at build time if you get them wrong:
#
#  1. tina4-nodejs ships its @tina4/* workspace packages as source with no
#     package.json of their own. They resolve only after scripts/link-tina4.mjs
#     (the postinstall) creates node_modules/@tina4, so scripts/ must be copied
#     BEFORE npm install and scripts must be allowed to run.
#
#  2. There is no compile step. `npm start` runs `tsx app.ts`, so tsx is a
#     RUNTIME dependency despite living in devDependencies. `npm ci --omit=dev`
#     builds an image that cannot start.

FROM node:22-alpine

WORKDIR /app

COPY package.json ./
COPY scripts/ ./scripts/

RUN npm install --no-audit --no-fund

COPY app.ts tsconfig.json ./
COPY src/ ./src/
# Static assets, and NOT optional. Sextant runs under `default-src 'self'`, so
# the stylesheet and every island script must be same-origin files — an inline
# <style> or <script> is blocked silently and the console renders unstyled and
# inert.
COPY public/ ./public/

# Sessions hold the bearer token. If this is not writable, sign-in appears to
# succeed and every subsequent request is unauthenticated, which reads as a
# backend fault rather than a filesystem one.
RUN mkdir -p /app/data/sessions && chown -R node:node /app/data

ENV NODE_ENV=production
ENV PORT=7148

EXPOSE 7148

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:7148/healthz >/dev/null 2>&1 || exit 1

USER node

CMD ["npm", "start"]
