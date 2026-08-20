# Image for running a CLI agent under test, isolated from the host.
#
# Contains the agent CLI (or CLIs) and the cluster tooling a DevOps task
# needs, and deliberately nothing else. The host repo, the operator's home
# directory, the Docker socket and ADC are all absent by construction rather
# than by policy: they are simply not in this image and not mounted by the
# sandbox wrapper.
#
#   docker build -f hack/agent-sandbox.Dockerfile -t devops-bench/agent-sandbox:dev .
#
# Pin GEMINI_CLI_VERSION/CLAUDE_CODE_CLI_VERSION for reproducible runs. Leaving
# either at "latest" means the agent under test changes underneath you between
# runs, which quietly makes results incomparable.
FROM node:22-slim

ARG KUBECTL_VERSION=v1.31.4
ARG GEMINI_CLI_VERSION=latest
ARG CLAUDE_CODE_CLI_VERSION=latest

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl jq git less \
 && rm -rf /var/lib/apt/lists/*

# kubectl, matched to the architecture the image is built for so this works on
# both arm64 laptops and amd64 CI.
RUN arch="$(dpkg --print-architecture)" \
 && curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
 && chmod 0755 /usr/local/bin/kubectl \
 && kubectl version --client=true >/dev/null

RUN npm install -g \
      "@google/gemini-cli@${GEMINI_CLI_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_CLI_VERSION}" \
 && npm cache clean --force

# openclaw (the `oc` binary) and Antigravity (the `agy` binary) are NOT
# installed here. Both agents/cli/openclaw and agents/cli/antigravity are now
# wired to honour BENCH_AGENT_SANDBOX (see devops_bench/agents/sandbox.py),
# but this image has no known public/scriptable install recipe for either
# CLI, so adding one here would be a guess rather than a verified build step.
# BENCH_AGENT_SANDBOX=docker is not usable for those two adapters against
# this image until whoever owns the internal openclaw/antigravity
# distribution adds the equivalent install RUN steps.

# The wrapper runs this container as the host user's uid:gid so files written to
# the mounted workspace are owned correctly. That uid does not exist in
# /etc/passwd, and some tooling resolves $HOME by looking the uid up rather than
# reading the env var, so give it a world-writable fallback. The wrapper also
# sets HOME=/workspace explicitly.
RUN mkdir -p /workspace /home/agent \
 && chmod 0777 /workspace /home/agent
ENV HOME=/workspace
WORKDIR /workspace

# No ENTRYPOINT on purpose: the sandbox wrapper appends the agent's own argv,
# which already begins with the binary name. An entrypoint here would silently
# prepend a second command.
CMD ["gemini", "--version"]
