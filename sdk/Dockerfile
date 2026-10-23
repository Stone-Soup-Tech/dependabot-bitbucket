FROM python:3.13-slim-bookworm

# Updater/proxy images are launched separately using Bitbucket's Docker service.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/dependabot-bitbucket
COPY versions.yml ./
COPY scripts/cli_release.py ./scripts/cli_release.py
RUN python3 scripts/cli_release.py --install /usr/local/bin/dependabot
COPY dependabot.py ./dependabot.py
RUN printf '#!/bin/sh\nexec python3 -B /opt/dependabot-bitbucket/dependabot.py "$@"\n' > /usr/local/bin/dependabot-bitbucket \
    && chmod 755 /usr/local/bin/dependabot-bitbucket
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
# No ENTRYPOINT: Bitbucket must be able to execute normal step shell scripts.
CMD ["/bin/bash"]
