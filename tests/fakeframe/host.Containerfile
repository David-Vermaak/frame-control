# The computer Frame Control runs on, for the e2e tests: Python 3.9 (the
# oldest the app supports), the OpenSSH client and rsync, and nothing else.
# The repository is mounted read-only at /repo (compose.yaml). OpenSSH reads
# ~/.ssh/config from the passwd home, not $HOME, which is why this is a
# container of its own rather than a HOME override on the machine running the tests.
FROM python:3.9-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client rsync procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /bin/bash tester
COPY host/entrypoint.sh /usr/local/bin/fakeframe-host
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER tester
WORKDIR /repo
CMD ["fakeframe-host"]
