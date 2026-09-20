ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

# stdlib-only service: no pip installs needed, no third-party dependencies
COPY evcharge /app/evcharge
COPY config.json /app/config.json
COPY run.sh /run.sh
RUN chmod a+x /run.sh && python3 -c "import sys; print(sys.version)"

WORKDIR /app
CMD ["/run.sh"]
