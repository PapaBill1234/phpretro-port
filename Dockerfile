FROM golang:1.26.9-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
COPY migrations ./migrations
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /cms ./cmd/cms

FROM alpine:3.23
RUN addgroup -g 10001 cms && adduser -D -u 10001 -G cms cms
WORKDIR /app
COPY --from=build /cms /app/cms
USER cms
EXPOSE 8080
ENTRYPOINT ["/app/cms"]
