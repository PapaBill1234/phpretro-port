FROM node:26.10.0-bookworm-slim AS build
WORKDIR /workspace/packages/cms-contracts
COPY packages/cms-contracts/package.json packages/cms-contracts/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY packages/cms-contracts/src ./src
COPY packages/cms-contracts/tsconfig.json packages/cms-contracts/tsconfig.build.json ./
RUN npm run build
WORKDIR /workspace/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY apps/web/ .
RUN npm run check && npm run build

FROM nginx:1.30.5-alpine
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/apps/web/dist /usr/share/nginx/html
EXPOSE 80
