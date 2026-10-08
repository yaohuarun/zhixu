FROM node:22.15.0-alpine AS build
WORKDIR /ui
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM nginx:1.28.0-alpine
COPY --from=build /ui/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
