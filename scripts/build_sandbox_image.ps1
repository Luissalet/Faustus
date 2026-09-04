# Build the image the agent's shell runs in.
#
# Faustus never pulls or builds an image on its own — a missing image is a
# refused run with this command in the message, not a silent download. Run this
# once, and after changing docker/sandbox.Dockerfile.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$tag = if ($args.Count -gt 0) { $args[0] } else { 'faustus-sandbox:1' }

Write-Output "Building $tag from docker/sandbox.Dockerfile ..."
docker build -f (Join-Path $repo 'docker\sandbox.Dockerfile') -t $tag $repo
if ($LASTEXITCODE -ne 0) { throw "docker build failed ($LASTEXITCODE)" }

Write-Output ''
Write-Output 'What the agent will find inside:'
docker run --rm --network none --user 1000:1000 $tag sh -lc `
  'echo "  python $(python --version 2>&1 | cut -d\" \" -f2)"; echo "  git    $(git --version | cut -d\" \" -f3)"; echo "  node   $(node --version)"; echo "  rg     $(rg --version | head -1 | cut -d\" \" -f2)"; echo "  jq     $(jq --version)"; echo "  curl   $(curl --version | head -1 | cut -d\" \" -f2)"'
