ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8
WORKDIR /app

COPY app/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app/ ./

# COPY rootfs without trailing slash — copies contents into /
COPY rootfs /

RUN find /etc/s6-overlay/s6-rc.d -name "run" -exec chmod a+x {} \;
