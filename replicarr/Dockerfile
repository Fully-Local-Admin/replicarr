ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8
WORKDIR /app

COPY app/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app/ ./
COPY rootfs/ /

RUN chmod a+x /etc/s6-overlay/s6-rc.d/replicarr/run
