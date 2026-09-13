"""Explicit cross-repository proof; actual producer bytes, private NATS, fixture GitHub."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import nats

from telemachy.fleet_publish import publish_registration
from telemachy.fleet_registration import canonical
from tests.integration.test_fleet_registration_transport import configure
from tests.test_fleet_registration import END, START, FakeGitHub, run


def outbox(github: FakeGitHub) -> dict[str, Any]:
    body = github.issues[42]["body"]
    block = body.split(START, 1)[1].split(END, 1)[0]
    return json.loads(block.split("```json\n", 1)[1].split("\n```", 1)[0])


async def exercise(url: str, executable: Path, output: Path) -> None:
    await configure(url)
    github = FakeGitHub()
    captures: list[dict[str, Any]] = []

    async def publish(subject: str, payload: dict[str, Any]) -> dict[str, Any]:
        persisted = outbox(github)
        assert persisted["phase"] == "publish_pending"
        assert persisted["envelope"] == payload
        receipt = await publish_registration(subject, payload, url, allow_insecure=True)
        nc = await nats.connect(url, allow_reconnect=False)
        try:
            message = await nc.jetstream().get_msg("homeric-pipeline", seq=receipt["seq"])
            assert message.subject == subject
            assert message.data == canonical(persisted["envelope"]).encode()
            captures.append(
                {"subject": subject, "payload": message.data.decode(), "receipt": receipt}
            )
        finally:
            await nc.close()
        if len(captures) == 1:
            # Broker committed the exact outbox bytes, but the following durable
            # published receipt cannot be written. A new invocation must replay.
            github.fail_write = github.writes + 1
        return receipt

    try:
        await run(github, publish)
    except RuntimeError as error:
        assert str(error) == "write unconfirmed"
    else:
        raise AssertionError("receipt-write failure did not propagate")
    assert github.creates == 2
    assert outbox(github)["phase"] == "publish_pending"
    saved_issues = copy.deepcopy(github.issues)
    github = FakeGitHub()
    github.issues = saved_issues  # A fresh caller reads the controlled durable authority.
    result = await run(github, publish)
    assert github.creates == 0
    assert result["phase"] == "published"
    assert len(captures) == 2
    assert captures[0]["payload"] == captures[1]["payload"]
    assert captures[1]["receipt"]["duplicate"] is True
    assert captures[0]["receipt"]["seq"] == captures[1]["receipt"]["seq"]
    assert await run(github, publish) == result
    assert len(captures) == 2
    capture = output / "producer-capture.json"
    capture.write_text(json.dumps(captures[0], sort_keys=True))
    (output / "producer-receipts.json").write_text(json.dumps(captures, sort_keys=True))

    # Failure control: the native harness must reject a changed capture even when
    # the real broker contains a valid epic. No work is authorized by this file.
    changed = copy.deepcopy(captures[0])
    changed["payload"] += " "
    mismatched = output / "mismatched-capture.json"
    mismatched.write_text(json.dumps(changed))
    env = {"PATH": os.defpath, "LANG": "C"}
    rejected = await asyncio.to_thread(
        subprocess.run,
        [str(executable), str(mismatched), url],
        env=env,
        capture_output=True,
        timeout=15,
        check=False,
    )
    (output / "negative-native.stdout").write_bytes(rejected.stdout)
    (output / "negative-native.stderr").write_bytes(rejected.stderr)
    assert rejected.returncode == 1
    assert b"capture differs from actual broker bytes" in rejected.stderr

    accepted = await asyncio.to_thread(
        subprocess.run,
        [str(executable), str(capture), url],
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    (output / "native.stdout").write_bytes(accepted.stdout)
    (output / "native.stderr").write_bytes(accepted.stderr)
    assert accepted.returncode == 0, accepted.stderr.decode()
    native = json.loads(accepted.stdout.splitlines()[-1])
    assert native["failedWriteFenced"] is True
    assert native["deliveries"] == 2 and native["replays"] == 1
    assert native["initialBackingIssues"] == 2
    assert native["exactBytes"] == len(captures[0]["payload"].encode())
    proof = {
        "schema": "hi/fleet/producer-native-proof/v1",
        "payloadSha256": hashlib.sha256(captures[0]["payload"].encode()).hexdigest(),
        "nativeBinarySha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "producerReceiptReplayed": True,
        "producerChildCreates": 2,
        "restartChildCreates": github.creates,
        "native": native,
        "scope": "private broker and controlled GitHub; no live admission or performance claim",
    }
    (output / "result.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps(proof, sort_keys=True))


def main() -> None:
    if not __debug__:
        raise RuntimeError("Contract assertions require a non-optimized Python runtime")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("native", type=Path)
    parser.add_argument("broker", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    native = args.native.resolve(strict=True)
    broker = args.broker.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="producer-native-broker-") as directory:
        root = Path(directory)
        with (output / "broker.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    str(broker),
                    "-a",
                    "127.0.0.1",
                    "-p",
                    "-1",
                    "-js",
                    "-sd",
                    directory,
                    "--ports_file_dir",
                    directory,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env={"PATH": os.defpath},
            )
            try:
                ports = root / f"nats-server_{process.pid}.ports"
                for _ in range(100):
                    if ports.exists():
                        break
                    if process.poll() is not None:
                        raise RuntimeError("private broker failed to start")
                    time.sleep(0.02)
                url = json.loads(ports.read_text())["nats"][0]
                assert url.startswith("nats://127.0.0.1:")
                asyncio.run(exercise(url, native, output))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
