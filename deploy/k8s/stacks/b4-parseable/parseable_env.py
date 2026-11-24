"""The parseable-env Secret the Parseable chart reads (fixed keys addr, username, password), copied from the
otb-credentials Secret the runtime applies, plus every key of STORE_SECRET when it exists (gke: gcs.url and
gcs.bucket). Writes only when the copy differs, so every `up` can run it (the Job deletes itself after it
finishes)."""

import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
NAMESPACE = Path(f"{SA_DIR}/namespace").read_text().strip()
API = f"https://kubernetes.default.svc/api/v1/namespaces/{NAMESPACE}/secrets"
AUTH = "Bearer " + Path(f"{SA_DIR}/token").read_text().strip()
TLS = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
TARGET = os.environ["TARGET_SECRET"]


def call(method: str, url: str, body: object = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": AUTH})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30, context=TLS) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


status, source = call("GET", f"{API}/{os.environ['SOURCE_SECRET']}")
if status != 200:
    sys.exit(f"GET secret {os.environ['SOURCE_SECRET']} -> HTTP {status}: {source}")
creds = {k: base64.b64decode(v).decode() for k, v in source["data"].items()}
want = {
    "addr": os.environ["P_ADDR"],
    "username": creds[os.environ["USERNAME_KEY"]],
    "password": creds[os.environ["PASSWORD_KEY"]],
}
status, store = call("GET", f"{API}/{os.environ['STORE_SECRET']}")
if status == 200:
    want |= {k: base64.b64decode(v).decode() for k, v in store["data"].items()}
elif status != 404:
    sys.exit(f"GET secret {os.environ['STORE_SECRET']} -> HTTP {status}: {store}")
data = {k: base64.b64encode(v.encode()).decode() for k, v in want.items()}
body = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": TARGET, "labels": {"otb.stack": "b4-parseable"}},
    "type": "Opaque",
    "data": data,
}

status, current = call("GET", f"{API}/{TARGET}")
if status == 200 and current.get("data") == data:
    print(f"{TARGET}: unchanged")
elif status == 200:
    status, out = call("PUT", f"{API}/{TARGET}", body)
    print(f"{TARGET}: updated" if status == 200 else sys.exit(f"PUT {TARGET} -> HTTP {status}: {out}"))
elif status == 404:
    status, out = call("POST", API, body)
    print(f"{TARGET}: created" if status == 201 else sys.exit(f"POST {TARGET} -> HTTP {status}: {out}"))
else:
    sys.exit(f"GET secret {TARGET} -> HTTP {status}: {current}")
