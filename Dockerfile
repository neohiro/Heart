FROM golang:1.22-alpine AS build
WORKDIR /src
# Build context = /opt/neohiro/heart/, Heart repo at /opt/neohiro/heart/Heart/.
COPY Heart/ .
RUN go build -o /out/heart ./Heart/cmd/heart

FROM alpine:3.20
RUN apk add --no-cache ca-certificates git bash
COPY --from=build /out/heart /usr/local/bin/heart
COPY Heart/docker/monitor.sh /usr/local/bin/monitor.sh
COPY Heart/scripts/heartbeat-sidecar.sh /usr/local/bin/heartbeat-sidecar.sh
COPY Heart/cmd/heart/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /usr/local/bin/monitor.sh /usr/local/bin/heartbeat-sidecar.sh /entrypoint.sh
USER 65532
ENTRYPOINT ["/entrypoint.sh"]
