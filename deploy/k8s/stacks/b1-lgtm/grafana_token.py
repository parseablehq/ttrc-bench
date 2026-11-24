"""A Viewer service account and token for mcp-grafana, kept in a Secret of this namespace. The same steps as
viewer_service_account in stacks/b1-lgtm/probes.py. A Secret whose token still authenticates is left alone, so
every `up` can run this Job."""

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
GRAFANA = os.environ["GRAFANA_URL"]
SA_NAME = os.environ["SA_NAME"]
SECRET, KEY = os.environ["SECRET_NAME"], os.environ["SECRET_KEY"]
ADMIN = "Basic " + base64.b64encode(f"{os.environ['ADMIN_USER']}:{os.environ['ADMIN_PASSWORD']}".encode()).decode()
NAMESPACE = Path(f"{SA_DIR}/namespace").read_text().strip()
KUBE = f"https://kubernetes.default.svc/api/v1/namespaces/{NAMESPACE}/secrets"
KUBE_AUTH = "Bearer " + Path(f"{SA_DIR}/token").read_text().strip()
KUBE_TLS = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")


def call(method: str, url: str, auth: str, body: object = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": auth})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    tls = KUBE_TLS if url.startswith("https://kubernetes.default.svc") else None
    try:
        with urllib.request.urlopen(req, timeout=30, context=tls) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    return status, json.loads(raw) if raw.strip() else None


def ok(method: str, url: str, auth: str, body: object = None) -> object:
    status, out = call(method, url, auth, body)
    if not 200 <= status < 300:
        sys.exit(f"{method} {url} -> HTTP {status}: {out}")
    return out


def wait_for_grafana(timeout_s: float = 900) -> None:
    deadline = time.monotonic() + timeout_s
    last: object = "no response"
    while True:
        try:
            last = call("GET", f"{GRAFANA}/api/health", ADMIN)[0]
            if last == 200:
                return
        except OSError as e:
            last = e
        if time.monotonic() > deadline:
            sys.exit(f"{GRAFANA} not healthy after {timeout_s}s: {last}")
        time.sleep(2)


def main() -> None:
    wait_for_grafana()
    status, secret = call("GET", f"{KUBE}/{SECRET}", KUBE_AUTH)
    if status == 200:
        token = base64.b64decode(secret["data"][KEY]).decode()  # type: ignore[index]
        if call("GET", f"{GRAFANA}/api/user", f"Bearer {token}")[0] == 200:
            print(f"{SECRET}: token still valid, unchanged")
            return
    elif status != 404:
        sys.exit(f"GET secret {SECRET} -> HTTP {status}: {secret}")

    found = ok("GET", f"{GRAFANA}/api/serviceaccounts/search?query={SA_NAME}", ADMIN)
    accounts = [a for a in found["serviceAccounts"] if a["name"] == SA_NAME]  # type: ignore[index]
    if accounts:
        sa_id = accounts[0]["id"]
    else:
        sa_id = ok("POST", f"{GRAFANA}/api/serviceaccounts", ADMIN, {"name": SA_NAME, "role": "Viewer"})["id"]  # type: ignore[index]
    name = f"{SA_NAME}-{int(time.time())}"
    token = ok("POST", f"{GRAFANA}/api/serviceaccounts/{sa_id}/tokens", ADMIN, {"name": name})

    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": SECRET, "labels": {"otb.stack": "b1-lgtm"}},
        "data": {KEY: base64.b64encode(token["key"].encode()).decode()},  # type: ignore[index]
    }
    if status == 200:
        ok("PUT", f"{KUBE}/{SECRET}", KUBE_AUTH, body)
    else:
        ok("POST", KUBE, KUBE_AUTH, body)
    print(f"{SECRET}: new token for service account {SA_NAME} (id {sa_id})")


main()
