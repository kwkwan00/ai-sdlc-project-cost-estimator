# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the Next.js wizard.
#   deps:    npm ci into /app/node_modules
#   builder: copy source + build with `next build` (output: 'standalone')
#   runtime: minimal node, copies only the standalone server bundle
#
# IMPORTANT: Next.js inlines NEXT_PUBLIC_* env vars at *build* time, not runtime.
# The default below points at http://localhost:8000 because the browser (not the
# container) is the API client — and the backend service publishes :8000 on the
# host. Override via the docker-compose `build.args.NEXT_PUBLIC_API_URL` if you
# expose the backend through a different hostname/port.
#
# Build context: ./frontend  (configured in docker-compose.yml)

FROM node:22-alpine AS deps

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund


FROM node:22-alpine AS builder

WORKDIR /app

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production

COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build


FROM node:22-alpine AS runtime

WORKDIR /app

RUN addgroup --system --gid 1001 app \
 && adduser --system --uid 1001 --ingroup app --home /app --shell /sbin/nologin app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Standalone output: self-contained server.js + minimal node_modules.
COPY --from=builder --chown=app:app /app/.next/standalone ./
COPY --from=builder --chown=app:app /app/.next/static ./.next/static
COPY --from=builder --chown=app:app /app/public ./public

USER app

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=6 \
  CMD wget -q --spider http://127.0.0.1:3000/ || exit 1

CMD ["node", "server.js"]
