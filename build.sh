#!/usr/bin/env bash
set -euo pipefail

IMAGE="yurirage/dock-of-the-base"
TAG="${1:-latest}"
PLATFORMS="linux/amd64,linux/arm64"
BUILDER="multiarch"

# Register QEMU binfmt handlers if arm64 is not available
if ! grep -q "^enabled" /proc/sys/fs/binfmt_misc/qemu-aarch64 2>/dev/null; then
    echo "Registering QEMU binfmt handlers..."
    docker run --privileged --rm tonistiigi/binfmt --install all
fi

# Create multi-platform builder if it doesn't exist
if ! docker buildx inspect "$BUILDER" &>/dev/null; then
    echo "Creating buildx builder '$BUILDER'..."
    docker buildx create --name "$BUILDER" --driver docker-container --bootstrap
fi

docker buildx build \
    --builder "$BUILDER" \
    --platform "$PLATFORMS" \
    --tag "$IMAGE:$TAG" \
    --push \
    .

echo
echo "Built and pushed $IMAGE:$TAG ($PLATFORMS)"
