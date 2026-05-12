#!/bin/sh

set -e

if [ ! -f "Dockerfile" ]; then
    echo "Cannot find Dockerfile in current folder"
    exit 1
fi

NAME="$(basename "$(pwd)")"

if [ -f "scripts/args.sh" ]; then
    . scripts/args.sh
fi

LAST_ID=$(docker ps -q --filter ancestor="$NAME")

if [ ! -z $LAST_ID ]; then
    docker stop $LAST_ID
fi
