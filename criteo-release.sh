#!/bin/bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd $DIR

# Versioning
VERSION="DUMMY-FOR-CI"
source VERSION
ORIGINAL_VERSION=$VERSION
LAST_COMMIT_ID=$(git rev-parse --short HEAD)
VERSION=$ORIGINAL_VERSION-criteo-$(date -u +%Y%m%d%H%M%S)-$LAST_COMMIT_ID
sed -i "s/$ORIGINAL_VERSION/$VERSION/g" VERSION

# Build release environment Docker image
docker build . -t hue-dev -f tools/docker/dev/Dockerfile

# Build Hue artifact
docker run --rm --volume $PWD:/data --workdir /data --user $(id -u):$(id -g) hue-dev make prod

# Release to Nexus
if [ ! -z $MAVEN_PASSWORD ]; then
    curl -v -u "$MAVEN_USER:$MAVEN_PASSWORD" --upload-file build/release/prod/hue-$VERSION.tgz http://nexus.criteo.prod/content/repositories/criteo.thirdparty/com/cloudera/hue/hue/$VERSION/hue-$VERSION.tgz
fi

