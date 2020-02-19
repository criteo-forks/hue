# build
```
docker build . -t hue-dev -f tools/docker/dev/Dockerfile
```

# run
```
docker run --rm -it --volume $PWD:/data --workdir /data --user $(id -u):$(id -g) hue-dev make prod
```
