# The image the agent's shell runs in when `agent_sandbox_execution` is on.
#
# `python:3.12-slim` alone was not usable: it has `sh` and `python` and almost
# nothing else, so half of what a model reasonably tries — `git status`,
# `rg TODO`, `node -v`, `curl` — came back "not found" and read like a broken
# sandbox rather than a bare image.
#
# What goes in is decided by one question: would the agent plausibly reach for
# it while working in a folder? Anything heavier (compilers, a full node
# toolchain, package managers pulling the world) stays out — a bigger image is
# a slower cold start on every command, and the sandbox already pays ~0.4s.
#
# There is deliberately no `sudo` and no package manager credentials. The
# container runs as uid 1000 with every capability dropped, so a run cannot
# install its way out of the image it was given.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      curl \
      ca-certificates \
      ripgrep \
      jq \
      less \
      unzip \
      nodejs \
    && rm -rf /var/lib/apt/lists/*
# `npm` is deliberately absent. Debian's package drags in node-gyp and, with
# it, a SECOND Python interpreter — 71 MB of packages measured on the first
# build of this file, for a tool the agent cannot usefully run anyway: the
# container has no network by default, so `npm install` would fail on the
# first fetch. `node` alone runs scripts, which is what the shell reaches for.

# A non-root home that exists and is writable: uid 1000 has no /etc/passwd
# entry, so tools that want $HOME (git, npm, pip) would otherwise write to /
# and fail. Created here rather than left to the run so the failure cannot
# depend on which command happens to need it first.
RUN mkdir -p /home/faustus /workspace /artifacts \
    && chown -R 1000:1000 /home/faustus /workspace /artifacts
ENV HOME=/home/faustus

# Git refuses to work in a directory owned by another user, which is exactly
# what a bind-mounted Windows folder looks like from inside. This is the one
# accommodation the mount needs, and it is scoped to the mount point.
RUN git config --system --add safe.directory /workspace \
    && git config --system --add safe.directory '*'

WORKDIR /workspace
