FROM node:22-alpine
WORKDIR /app
COPY package.json ./
COPY . .
ENV PORT=5173
EXPOSE 5173
CMD ["node", "backend/server.mjs"]
