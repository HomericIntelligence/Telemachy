"""Real private JetStream producer tests; all GitHub calls use a controlled fixture."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import nats
import pytest
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

from telemachy.fleet_publish import publish_registration
from tests.test_fleet_registration import FakeGitHub, run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture()
def private_broker() -> Iterator[str]:
    executable = shutil.which(os.environ.get("TELEMACHY_TEST_NATS_SERVER", "nats-server"))
    if not executable:
        pytest.fail("An existing nats-server binary is required; this test never installs one")
    with tempfile.TemporaryDirectory(prefix="telemachy-js-") as directory:
        root = Path(directory)
        with (root / "broker.log").open("wb") as output:
            process = subprocess.Popen(
                [
                    executable,
                    "-a",
                    "127.0.0.1",
                    "-p",
                    "-1",
                    "-js",
                    "-sd",
                    str(root / "storage"),
                    "--ports_file_dir",
                    directory,
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                env={"PATH": os.defpath},
            )
            try:
                ports = root / f"nats-server_{process.pid}.ports"
                for _ in range(100):
                    if ports.exists():
                        break
                    if process.poll() is not None:
                        pytest.fail("Private broker failed to start")
                    time.sleep(0.02)
                url = json.loads(ports.read_text())["nats"][0]
                assert url.startswith("nats://127.0.0.1:")
                yield url
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


async def configure(url: str) -> None:
    nc = await nats.connect(url, allow_reconnect=False)
    try:
        await nc.jetstream().add_stream(
            config=StreamConfig(
                name="homeric-pipeline",
                subjects=["hi.pipeline.>"],
                storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS,
                discard=DiscardPolicy.NEW,
                max_age=0,
                duplicate_window=120,
                max_bytes=10 * 1024 * 1024,
            )
        )
    finally:
        await nc.close()


async def test_real_puback_and_stable_message_dedup(private_broker: str) -> None:
    await configure(private_broker)
    payload = {
        "schema": "hi/v1",
        "msg_id": "producer-proof-1",
        "epic": {"repo": "Homeric/repo", "issue": 99, "key": "homeric-repo-99"},
        "children": [101],
        "workflow": "proof",
    }
    subject = "hi.pipeline.epic.homeric-repo-99.registered"
    first = await publish_registration(subject, payload, private_broker, allow_insecure=True)
    second = await publish_registration(subject, payload, private_broker, allow_insecure=True)
    assert first["stream"] == "homeric-pipeline"
    assert first["seq"] > 0
    assert second["seq"] == first["seq"]
    assert second["duplicate"] is True


async def test_puback_then_failed_github_receipt_replays_without_new_issues(
    private_broker: str,
) -> None:
    await configure(private_broker)
    gh = FakeGitHub()
    receipts: list[dict[str, Any]] = []

    async def publisher(subject: str, payload: dict[str, Any]) -> dict[str, Any]:
        receipt = await publish_registration(subject, payload, private_broker, allow_insecure=True)
        receipts.append(receipt)
        if len(receipts) == 1:
            gh.fail_write = gh.writes + 1
        return receipt

    with pytest.raises(RuntimeError, match="write unconfirmed"):
        await run(gh, publisher)
    result = await run(gh, publisher)
    assert result["phase"] == "published"
    assert gh.creates == 2
    assert len(receipts) == 2
    assert receipts[0]["seq"] == receipts[1]["seq"]
    assert receipts[1]["duplicate"] is True


async def test_missing_stream_cannot_fall_back_to_core_publish(private_broker: str) -> None:
    payload = {"schema": "hi/v1", "msg_id": "missing-stream", "epic": {"key": "homeric-repo-99"}}
    with pytest.raises(nats.js.errors.NotFoundError):
        await publish_registration(
            "hi.pipeline.epic.homeric-repo-99.registered",
            payload,
            private_broker,
            allow_insecure=True,
        )


async def test_expiring_stream_is_rejected_before_publication(private_broker: str) -> None:
    await configure(private_broker)
    nc = await nats.connect(private_broker, allow_reconnect=False)
    try:
        js = nc.jetstream()
        info = await js.stream_info("homeric-pipeline")
        info.config.max_age = 3600
        await js.update_stream(config=info.config)
        payload = {
            "schema": "hi/v1",
            "msg_id": "expiring-stream",
            "epic": {"key": "homeric-repo-99"},
        }
        with pytest.raises(RuntimeError, match="incompatible_durable_pipeline_stream"):
            await publish_registration(
                "hi.pipeline.epic.homeric-repo-99.registered",
                payload,
                private_broker,
                allow_insecure=True,
            )
        assert (await js.stream_info("homeric-pipeline")).state.messages == 0
    finally:
        await nc.close()


@pytest.mark.parametrize("discard_new_per_subject", [False, True])
async def test_subject_limit_never_evicts_prior_registration(
    private_broker: str, discard_new_per_subject: bool
) -> None:
    await configure(private_broker)
    nc = await nats.connect(private_broker, allow_reconnect=False)
    subject = "hi.pipeline.epic.a-b-c-99.registered"
    first = b'{"retained":"registration from a-b/c"}'
    try:
        js = nc.jetstream()
        info = await js.stream_info("homeric-pipeline")
        info.config.max_msgs_per_subject = 1
        info.config.discard_new_per_subject = discard_new_per_subject
        await js.update_stream(config=info.config)
        retained = await js.publish(subject, first)
        payload = {
            "schema": "hi/v1",
            "msg_id": "distinct-registration",
            "epic": {"repo": "a/b-c", "issue": 99, "key": "a-b-c-99"},
        }
        if discard_new_per_subject:
            with pytest.raises(nats.js.errors.APIError):
                await publish_registration(subject, payload, private_broker, allow_insecure=True)
        else:
            with pytest.raises(RuntimeError, match="incompatible_durable_pipeline_stream"):
                await publish_registration(subject, payload, private_broker, allow_insecure=True)
        assert (await js.get_msg("homeric-pipeline", seq=retained.seq)).data == first
        assert (await js.stream_info("homeric-pipeline")).state.messages == 1
    finally:
        await nc.close()
