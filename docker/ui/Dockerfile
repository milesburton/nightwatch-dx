FROM node:22-slim AS builder

ARG APP_VERSION=v0.0.0
ENV APP_VERSION=${APP_VERSION}

WORKDIR /app
COPY ui/package*.json ./
RUN npm install
COPY ui/ .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY docker/ui/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
