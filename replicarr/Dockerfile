ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8
WORKDIR /app

COPY app/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app/ ./
COPY rootfs/ /

RUN chmod a+x /init \
 && find /etc/s6-overlay/s6-rc.d -name "run" -exec chmod a+x {} \; \
 && find /etc/s6-overlay/s6-rc.d -name "finish" -exec chmod a+x {} \;

CMD ["/init"]
