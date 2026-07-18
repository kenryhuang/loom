from __future__ import annotations

import asyncio
from pathlib import Path

from loom.campaigns.operations import CampaignOperation
from loom.campaigns.serialization import canonical_digest, new_prefixed_id

from .conftest import campaign_actor, make_campaign_spec, make_campaign_store


def test_campaign_store_enforces_writer_roles_and_separation(tmp_path: Path):
    async def scenario():
        store = make_campaign_store(tmp_path / "campaign")
        spec = make_campaign_spec(store)

        denied_create = await store.create(
            spec,
            operation_id=new_prefixed_id("op_"),
            actor=campaign_actor("campaign_controller"),
        )
        assert not denied_create.ok and denied_create.error.code == "AUTHORIZATION_FAILED"

        created = await store.create(
            spec,
            operation_id=new_prefixed_id("op_"),
            actor=campaign_actor("campaign_creator"),
        )
        assert created.ok
        finalizer_experiment = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("experiment"),
                "experiment.completed",
                campaign_actor("campaign_finalizer"),
                payload={"candidate_id": "missing", "score": {}},
            ),
            created.value.aggregate_version,
        )
        controller_finalization = await store.transact(
            CampaignOperation(
                new_prefixed_id("op_"),
                spec.campaign_id,
                canonical_digest("finalize"),
                "campaign.finalized",
                campaign_actor("campaign_controller"),
            ),
            created.value.aggregate_version,
        )

        assert not finalizer_experiment.ok and finalizer_experiment.error.code == "AUTHORIZATION_FAILED"
        assert not controller_finalization.ok and controller_finalization.error.code == "AUTHORIZATION_FAILED"

    asyncio.run(scenario())
