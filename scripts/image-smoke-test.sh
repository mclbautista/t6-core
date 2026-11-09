#!/usr/bin/env sh
# Smoke-test a built t6 image: `t6 version` runs, and the default command
# (the server) answers on its public listener. Usage: image-smoke-test.sh IMAGE
set -eu
image="${1:?usage: image-smoke-test.sh IMAGE}"

docker run --rm "$image" t6 version | grep -q '"version"'

name="t6-smoke-$$"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$name" -e T6_PUBLIC_BIND=0.0.0.0:8080 "$image" >/dev/null
for _ in $(seq 1 20); do
    status=$(docker exec "$name" python -c \
        "import urllib.request as u, urllib.error as e
try: u.urlopen('http://127.0.0.1:8080/')
except e.HTTPError as x: print(x.code)" 2>/dev/null || true)
    if [ -n "$status" ]; then
        echo "server answered HTTP $status"
        exit 0
    fi
    sleep 1
done
docker logs "$name" >&2
echo "server did not answer" >&2
exit 1
