"""JetStream acknowledgment boundary for Fleet registration."""

from typing import Any
from urllib.parse import urlparse

from telemachy.fleet_registration import Receipt, RegistrationError, canonical


async def publish_registration(
    subject: str,
    payload: dict[str, Any],
    nats_url: str,
    *,
    token: str | None = None,
    allow_insecure: bool = False,
) -> dict[str, Any]:
    import nats
    from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType

    if urlparse(nats_url).scheme != "tls" and not allow_insecure:
        raise RegistrationError("fleet_nats_tls_required")
    if (
        payload.get("schema") != "hi/v1"
        or not isinstance(payload.get("msg_id"), str)
        or not payload["msg_id"]
    ):
        raise RegistrationError("invalid_registration_envelope")
    if subject != "hi.pipeline.epic." + payload.get("epic", {}).get("key", "") + ".registered":
        raise RegistrationError("invalid_registration_subject")
    nc = await nats.connect(nats_url, token=token, allow_reconnect=False, connect_timeout=3)
    try:
        js = nc.jetstream()
        info = await js.stream_info("homeric-pipeline")
        config = info.config
        if (
            config.storage != StorageType.FILE
            or config.retention != RetentionPolicy.LIMITS
            or config.discard != DiscardPolicy.NEW
            or config.max_age != 0
            or config.duplicate_window is None
            or config.duplicate_window < 120
        ):
            raise RegistrationError("incompatible_durable_pipeline_stream")
        ack = await js.publish(
            subject,
            canonical(payload).encode(),
            timeout=3,
            headers={"Nats-Msg-Id": payload["msg_id"], "Nats-Expected-Stream": "homeric-pipeline"},
        )
        return Receipt.model_validate(
            {"stream": ack.stream, "seq": ack.seq, "duplicate": ack.duplicate or False}
        ).model_dump()
    finally:
        await nc.close()
