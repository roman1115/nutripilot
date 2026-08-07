FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm config set registry "$NPM_REGISTRY" && npm ci
COPY . .

ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD wget -q -O - http://127.0.0.1/healthz || exit 1
