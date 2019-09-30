#!/bin/bash

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd $DIR

# Versioning
NEXUS_URL="http://nexus.criteo.prod/content/repositories/criteo.thirdparty/com/cloudera/hue/hue"
VERSION="DUMMY-FOR-CI"
source VERSION
ORIGINAL_VERSION=$VERSION
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
LAST_COMMIT_ID=$(git rev-parse --short HEAD)
DATE=$(date -u +%Y%m%d%H%M%S)
VERSION=$GIT_BRANCH-$DATE-$LAST_COMMIT_ID
sed -i "s/$ORIGINAL_VERSION/$VERSION/g" VERSION

# Build release environment Docker image
docker build . -t hue-dev -f tools/docker/dev/Dockerfile

# Build Hue artifact
docker run --rm --volume $PWD:/data --workdir /data --user $(id -u):$(id -g) hue-dev make prod

# Release to Nexus
if [ ! -z $MAVEN_PASSWORD ]; then
    curl -v -u "$MAVEN_USER:$MAVEN_PASSWORD" --upload-file build/release/prod/hue-*.tgz ${NEXUS_URL}/$VERSION/hue-$VERSION.tgz
    cat << EOF > maven-metadata.xml
<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <groupId>com.cloudera.hue</groupId>
  <artifactId>hue</artifactId>
  <versioning>
    <latest>$VERSION</latest>
    <release>$VERSION</release>
    <versions>
      <version>$VERSION</version>
    </versions>
    <lastUpdated>$DATE</lastUpdated>
  </versioning>
</metadata>
EOF
    curl -v -u "$MAVEN_USER:$MAVEN_PASSWORD" -X DELETE ${NEXUS_URL}/maven-metadata.xml
    curl -v -u "$MAVEN_USER:$MAVEN_PASSWORD" -X DELETE ${NEXUS_URL}/maven-metadata.xml.md5
    curl -v -u "$MAVEN_USER:$MAVEN_PASSWORD" -X DELETE ${NEXUS_URL}/maven-metadata.xml.sha1
    curl -v -u "$MAVEN_USER:$MAVEN_PASSWORD" --upload-file maven-metadata.xml ${NEXUS_URL}/maven-metadata.xml
fi
