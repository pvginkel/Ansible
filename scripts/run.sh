#!/bin/sh

set -e

. "$(dirname "$0")/build.sh"

if [ $# -gt 0 ]; then
    docker run --rm -it $ARGS "$NAME:latest" $*
else
    ID=$(docker run --rm -d $ARGS "$NAME:latest")
    docker logs $ID -f
fi
