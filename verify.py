#!/usr/bin/env python3
"""verify.py — Smoke-test tracker-server after set_scenario.sh.

Sends test CoAP messages with the appropriate credentials for the given scenario
and prints PASS / FAIL for each check.  Reads credentials automatically from
tracker-server/.env and tracker-server/oscore-context/.

Usage:
    python3 scripts/verify.py --coap S1
    python3 scripts/verify.py --coap S2
    python3 scripts/verify.py --coap S3
    python3 scripts/verify.py --coap S4
    python3 scripts/verify.py --coap S2 --check-influx

CoAP scenarios:
    S1  Plain CoAP, port 5683   — verifies plain POST is accepted (2.04)
    S2  OSCORE, port 5683       — verifies plain rejected, correct key accepted, wrong key rejected
    S3  DTLS, port 5684         — verifies plain CoAPS rejected, PSK-authenticated POST accepted
    S4  DTLS + OSCORE, port 5684 — verifies DTLS-only rejected, DTLS+OSCORE accepted

Options:
    --host HOST         Override server hostname (default: read from hub-lte local.conf)
    --oscore-ctx DIR    Override OSCORE context dir (default: tracker-server/oscore-context)
    --check-influx      Query InfluxDB for recently written verify-test data
    --influx-url URL    InfluxDB URL override (default: from tracker-server/.env)
    --influx-token T    InfluxDB token override
    --influx-org ORG    InfluxDB org override
    --influx-bucket B   InfluxDB bucket override

Requirements (install with pip):
    aiocoap[oscore,tinydtls]>=0.4.7
    cbor2
    influxdb-client[async]  (only for --check-influx)
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

import aiocoap
import cbor2

WORKSPACE = Path(__file__).resolve().parent.parent
SERVER_ENV = WORKSPACE / "tracker-server" / ".env"
SERVER_TESTING_ENV = WORKSPACE / "tracker-server" / ".env.testing"
HUB_LTE_CONF = WORKSPACE / "tracker-hub" / "apps" / "tracker-hub-lte" / "local.conf"
DEFAULT_OSCORE_CTX = WORKSPACE / "tracker-server" / "oscore-context"

_pass_count = 0
_fail_count = 0


def _pass(label: str, code: object) -> None:
    global _pass_count
    _pass_count += 1
    print(f"  PASS  {label} ({code})")


def _fail(label: str, expected: str, got: object) -> None:
    global _fail_count
    _fail_count += 1
    print(f"  FAIL  {label} — expected {expected}, got {got}")


def read_kv(path: Path) -> dict:
    kv = {}
    if not path.exists():
        return kv
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            kv[key.strip()] = val.strip().strip('"')
    return kv


def env_payload(imei: str = "verify-test") -> bytes:
    return cbor2.dumps({
        "type": "env",
        "imei": imei,
        "t": 22.5,
        "ts": 1748000000000,
    })


def _make_client_ctx(server_ctx_dir: Path, out_dir: Path) -> Path:
    """Derive client-side OSCORE context by swapping sender/recipient IDs."""
    server_secret = json.loads((server_ctx_dir / "secret.json").read_text())
    client_secret = {
        "secret_hex": server_secret["secret_hex"],
        "salt_hex": server_secret["salt_hex"],
        "sender-id_hex": server_secret["recipient-id_hex"],
        "recipient-id_hex": server_secret["sender-id_hex"],
    }
    ctx_path = out_dir / server_ctx_dir.name
    ctx_path.mkdir(parents=True, exist_ok=True)
    fd = os.open(ctx_path / "secret.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(client_secret, f, indent=2)
    return ctx_path


def _make_wrong_ctx(out_dir: Path, sender: str = "01") -> Path:
    wrong_secret = {
        "secret_hex": secrets.token_bytes(16).hex(),
        "salt_hex": secrets.token_bytes(8).hex(),
        "sender-id_hex": sender,
        "recipient-id_hex": "00",
    }
    ctx_path = out_dir / "wrong"
    ctx_path.mkdir(parents=True, exist_ok=True)
    fd = os.open(ctx_path / "secret.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(wrong_secret, f, indent=2)
    return ctx_path


async def _post(uri: str, payload: bytes, creds: dict | None = None) -> object:
    ctx = await aiocoap.Context.create_client_context()
    try:
        if creds:
            ctx.client_credentials.load_from_dict(creds)
        req = aiocoap.Message(code=aiocoap.POST, uri=uri, payload=payload)
        req.opt.content_format = 60  # application/cbor
        resp = await asyncio.wait_for(ctx.request(req).response, timeout=15)
        return resp.code
    except asyncio.TimeoutError:
        return "timeout"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc!s:.80}"
    finally:
        await ctx.shutdown()


# ── scenario verifiers ────────────────────────────────────────────────────────

async def verify_s1(host: str) -> None:
    uri = f"coap://{host}/data"
    print(f"S1: plain CoAP → {uri}")
    code = await _post(uri, env_payload())
    if code == aiocoap.CHANGED:
        _pass("plain POST accepted", code)
    else:
        _fail("plain POST", "2.04 Changed", code)


async def verify_s2(host: str, oscore_ctx: Path) -> None:
    uri = f"coap://{host}/data"
    print(f"S2: OSCORE → {uri}")

    hub_ctx = oscore_ctx / "hub"
    if not hub_ctx.exists():
        print(f"  SKIP  no hub context at {hub_ctx}")
        print(f"        Run: cd tracker-server && python3 generate_oscore_psk.py --name hub")
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client_ctx = _make_client_ctx(hub_ctx, tmp_path)
        wrong_ctx = _make_wrong_ctx(tmp_path)

        code = await _post(uri, env_payload())
        if code == aiocoap.UNAUTHORIZED:
            _pass("plain CoAP rejected", code)
        else:
            _fail("plain CoAP should be rejected", "4.01 Unauthorized", code)

        oscore_creds = {uri: {"oscore": {"contextfile": str(client_ctx)}}}
        code = await _post(uri, env_payload(), creds=oscore_creds)
        if code == aiocoap.CHANGED:
            _pass("correct-key OSCORE accepted", code)
        else:
            _fail("correct-key OSCORE", "2.04 Changed", code)

        wrong_creds = {uri: {"oscore": {"contextfile": str(wrong_ctx)}}}
        code = await _post(uri, env_payload(), creds=wrong_creds)
        if code == aiocoap.UNAUTHORIZED:
            _pass("wrong-key OSCORE rejected", code)
        else:
            _fail("wrong-key OSCORE should be rejected", "4.01 Unauthorized", code)


async def verify_s3(host: str, dtls_psk: bytes, dtls_identity: bytes) -> None:
    dtls_uri = f"coaps://{host}:5684/data"
    print(f"S3: DTLS → {dtls_uri}")

    # Plain CoAP to the DTLS port should fail (no handshake)
    plain_uri = f"coap://{host}:5684/data"
    code = await _post(plain_uri, env_payload())
    if not (isinstance(code, str) and code.startswith("error")) and code != "timeout":
        _fail("plain CoAP on DTLS port should fail", "error/timeout", code)
    else:
        _pass("plain CoAP on DTLS port rejected", code)

    dtls_creds = {
        f"coaps://{host}:5684/*": {
            "dtls": {"psk": dtls_psk, "client-identity": dtls_identity},
        }
    }
    code = await _post(dtls_uri, env_payload(), creds=dtls_creds)
    if code == aiocoap.CHANGED:
        _pass("DTLS PSK accepted", code)
    else:
        _fail("DTLS PSK", "2.04 Changed", code)


async def verify_s4(host: str, dtls_psk: bytes, dtls_identity: bytes, oscore_ctx: Path) -> None:
    dtls_uri = f"coaps://{host}:5684/data"
    print(f"S4: DTLS+OSCORE → {dtls_uri}")

    hub_ctx = oscore_ctx / "hub"
    if not hub_ctx.exists():
        print(f"  SKIP  no hub context at {hub_ctx}")
        print(f"        Run: cd tracker-server && python3 generate_oscore_psk.py --name hub")
        return

    dtls_creds = {
        f"coaps://{host}:5684/*": {
            "dtls": {"psk": dtls_psk, "client-identity": dtls_identity},
        }
    }

    # DTLS-only (no OSCORE) should be rejected when server requires OSCORE too
    code = await _post(dtls_uri, env_payload(), creds=dtls_creds)
    if code == aiocoap.UNAUTHORIZED:
        _pass("DTLS-only (no OSCORE) rejected", code)
    else:
        _fail("DTLS-only should be rejected", "4.01 Unauthorized", code)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        client_ctx = _make_client_ctx(hub_ctx, tmp_path)

        combined_creds = {
            **dtls_creds,
            dtls_uri: {"oscore": {"contextfile": str(client_ctx)}},
        }
        code = await _post(dtls_uri, env_payload(), creds=combined_creds)
        if code == aiocoap.CHANGED:
            _pass("DTLS+OSCORE accepted", code)
        else:
            _fail("DTLS+OSCORE", "2.04 Changed", code)


async def check_influx(url: str, token: str, org: str, bucket: str) -> None:
    try:
        from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
    except ImportError:
        print("  SKIP  influxdb-client not installed (pip install influxdb-client[async])")
        return

    imei = "verify-test"
    query = f'''
from(bucket: "{bucket}")
  |> range(start: -5m)
  |> filter(fn: (r) => r["imei"] == "{imei}")
  |> last()
'''
    try:
        async with InfluxDBClientAsync(url=url, token=token, org=org) as client:
            tables = await client.query_api().query(query)
            if any(tables):
                _pass(f"InfluxDB: data for imei={imei!r} found in last 5 min", "present")
            else:
                print(f"  WARN  InfluxDB: no data for imei={imei!r} in last 5 min")
                print(f"        (allow ~10 s after the CoAP POST for InfluxDB write)")
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP  InfluxDB unreachable: {exc!s:.120}")
        print(f"        (normal if InfluxDB is only on the Docker-internal network)")


# ── entry point ───────────────────────────────────────────────────────────────

async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Post-scenario smoke test for tracker-server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--coap", required=True, choices=["S1", "S2", "S3", "S4"],
                    help="CoAP scenario to verify (matches set_scenario.sh --coap)")
    ap.add_argument("--host",
                    help="Server hostname (default: CONFIG_APP_COAP_SERVER_HOST from hub-lte local.conf)")
    ap.add_argument("--oscore-ctx", type=Path, default=DEFAULT_OSCORE_CTX, metavar="DIR",
                    help="OSCORE context directory (default: tracker-server/oscore-context)")
    ap.add_argument("--check-influx", action="store_true",
                    help="Query InfluxDB for recently written verify-test data after CoAP tests")
    ap.add_argument("--influx-url", metavar="URL")
    ap.add_argument("--influx-token", metavar="TOKEN")
    ap.add_argument("--influx-org", metavar="ORG")
    ap.add_argument("--influx-bucket", metavar="BUCKET")
    args = ap.parse_args()

    # .env has infra secrets; .env.testing has scenario config — merge with testing taking precedence
    env = {**read_kv(SERVER_ENV), **read_kv(SERVER_TESTING_ENV)}
    hub_conf = read_kv(HUB_LTE_CONF)

    host = args.host or hub_conf.get("CONFIG_APP_COAP_SERVER_HOST", "127.0.0.1")
    oscore_ctx = args.oscore_ctx

    dtls_psk_hex = env.get("DTLS_PSK_KEY_HEX", "")
    dtls_identity = env.get("DTLS_PSK_IDENTITY", "tracker-hub")

    print(f"Host     : {host}")
    print(f"Scenario : {args.coap}")
    print()

    if args.coap == "S1":
        await verify_s1(host)
    elif args.coap == "S2":
        await verify_s2(host, oscore_ctx)
    elif args.coap == "S3":
        if not dtls_psk_hex:
            print("ERROR: DTLS_PSK_KEY_HEX not set in tracker-server/.env")
            print("       Run: cd tracker-server && python3 generate_dtls_psk.py")
            return 1
        await verify_s3(host, bytes.fromhex(dtls_psk_hex), dtls_identity.encode())
    elif args.coap == "S4":
        if not dtls_psk_hex:
            print("ERROR: DTLS_PSK_KEY_HEX not set in tracker-server/.env")
            print("       Run: cd tracker-server && python3 generate_dtls_psk.py")
            return 1
        await verify_s4(host, bytes.fromhex(dtls_psk_hex), dtls_identity.encode(), oscore_ctx)

    if args.check_influx:
        print()
        print("InfluxDB:")
        influx_url = args.influx_url or env.get("INFLUXDB_URL", "")
        influx_token = args.influx_token or env.get("INFLUXDB_TOKEN", "")
        influx_org = args.influx_org or env.get("INFLUXDB_ORG", "")
        influx_bucket = args.influx_bucket or env.get("INFLUXDB_BUCKET", "iot_data")
        if not all([influx_url, influx_token, influx_org]):
            print("  SKIP  INFLUXDB_URL / INFLUXDB_TOKEN / INFLUXDB_ORG not configured")
        else:
            await check_influx(influx_url, influx_token, influx_org, influx_bucket)

    print()
    total = _pass_count + _fail_count
    if _fail_count:
        print(f"{_fail_count}/{total} check(s) FAILED")
        return 1
    print(f"All {total} check(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
