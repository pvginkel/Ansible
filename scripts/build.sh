#!/bin/sh

set -e

. "$(dirname "$0")/stop.sh"

docker build $BUILD_ARGS -t "$NAME":latest .
