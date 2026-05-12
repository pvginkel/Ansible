#!/bin/sh

set -e

. "$(dirname "$0")/build.sh"

docker tag "$NAME" "registry:5000/$NAME"
docker push "registry:5000/$NAME"
