# Use official Node.js LTS image
FROM node:22-alpine AS base

WORKDIR /app

COPY package.json package-lock.json* ./

# Development stage
FROM base AS development
RUN npm install --legacy-peer-deps
COPY . .
# compose runs this as the host UID so that .nuxt/ and any lockfile the dev
# server writes into the bind-mounted ./front are not root-owned. node_modules
# is an anonymous volume seeded from this path, so it must be chowned too or the
# Vite dep cache under it is unwritable.
RUN chown -R 1000:1000 /app
EXPOSE 3000
CMD ["npm", "run", "dev"]

# Production stage
FROM base AS production
ARG NUXT_PUBLIC_API_BASE_URL
ENV NUXT_PUBLIC_API_BASE_URL=${NUXT_PUBLIC_API_BASE_URL}
RUN npm install --legacy-peer-deps
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["npm", "run", "start"]
