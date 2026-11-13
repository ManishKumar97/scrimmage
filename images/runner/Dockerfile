# The Scrimmage runner: the product's image, with no team code in it.
# Nothing is fetched while it builds: the wheels are staged first and the build runs with
# `docker build --network none`. No ARG or ENV here holds a secret.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY wheels/ /wheels/
RUN pip install --no-index --find-links /wheels --no-cache-dir scrimmage-core
USER 10001:10001
ENTRYPOINT ["scrimmage"]
