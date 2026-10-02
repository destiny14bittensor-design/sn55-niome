# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2025 Genomes.io
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.


import asyncio
import hashlib
import logging
import os
import sys
import threading
import time

from niome_subnet.base.miner import BaseMinerNeuron
from niome_subnet.genomics.model import Task
from niome_subnet.genomics.builder_policy import resolve_builder_policy
from niome_subnet.miner import (
    pending_task_envelopes,
    persist_runtime_policy,
    persist_task_envelope,
    process_live_task,
)
from niome_subnet.protocol import GenomicsTaskSynapse

logger = logging.getLogger(__name__)

# Add project root to Python path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class Miner(BaseMinerNeuron):
    """
    Miner neuron. Receives genomics tasks from validators via HTTP and processes them.
    """

    MAX_RETRIES = 3

    def __init__(self, config=None):
        super(Miner, self).__init__(config=config)
        self.artifact_root = os.getenv("NIOME_ARTIFACT_ROOT", "artifacts/live")
        self.builder_policy = resolve_builder_policy()
        persist_runtime_policy(
            artifact_root=self.artifact_root,
            builder_policy=self.builder_policy,
            axon_port=self.axon_port,
        )
        pending = pending_task_envelopes(self.artifact_root)
        if pending:
            logger.warning("Resuming %d interrupted live task(s)", len(pending))
            threading.Thread(
                target=self._resume_pending_tasks,
                args=(pending,),
                daemon=True,
                name="niome-live-task-recovery",
            ).start()

    def _resume_pending_tasks(self, pending) -> None:
        for task, presigned_url, caller_hotkey in pending:
            try:
                process_live_task(
                    task=task,
                    presigned_url=presigned_url,
                    caller_hotkey=caller_hotkey,
                    artifact_root=self.artifact_root,
                    round_seeds=None,
                    builder_policy=self.builder_policy,
                )
            except Exception as error:
                logger.error("Recovered task %s failed: %s", task.id, error)

    async def forward(self, body: bytes, caller_hotkey: str) -> dict:
        """
        Processes an incoming genomics task request.

        Args:
            body: Raw JSON body bytes from the validator.
            caller_hotkey: Verified hotkey ss58 of the calling validator.

        Returns:
            dict: Response payload (empty acknowledgement).
        """
        try:
            synapse = GenomicsTaskSynapse.model_validate_json(body)
            task_data = synapse.task.model_dump()
            logger.info(f"Received genomics task: {task_data}")

            # Persist the upload credential before acknowledging the request.
            # A restart between the ACK and the worker's first instruction must
            # not turn a successfully delivered task into a missed round.
            persist_task_envelope(
                task=synapse.task,
                presigned_url=synapse.presigned_url,
                caller_hotkey=caller_hotkey,
                artifact_root=self.artifact_root,
            )

            # Fire and forget - run process_task asynchronously without waiting
            asyncio.create_task(
                self.process_task(synapse.task, synapse.presigned_url, caller_hotkey)
            )

            return {}
        except Exception as e:
            logger.error(f"Forward error: {e}")
            return {"error": str(e)}

    async def process_task(
        self, task: Task, presigned_url: str, caller_hotkey: str
    ) -> None:
        """Immediately build and upload an honest unknown-seed submission.

        Block-derived/provisional seeds drifted from the validator's official
        seed authority.  Waiting for them only burns the presigned URL budget,
        so the first-submission portfolio starts as soon as the request is
        durably captured.
        """
        try:
            await asyncio.to_thread(
                process_live_task,
                task=task,
                presigned_url=presigned_url,
                caller_hotkey=caller_hotkey,
                artifact_root=self.artifact_root,
                round_seeds=None,
                builder_policy=self.builder_policy,
            )
        except Exception as error:
            logger.error("Task %s failed: %s", task.id, error)

    def _generate_signature(self, answer_str: str, confidence: float) -> str:
        """Generate cryptographic signature for answer."""
        data = f"{answer_str}:{confidence}:{time.time()}"
        signature = hashlib.sha256(data.encode()).hexdigest()
        return signature

    async def blacklist(self, caller_hotkey: str) -> bool:
        """
        Determines whether an incoming request should be blacklisted.

        Args:
            caller_hotkey: ss58 hotkey of the caller (already verified by http_auth).

        Returns:
            bool: True if the request should be rejected.
        """
        if caller_hotkey not in self.metagraph.hotkeys:
            if not self.config.blacklist.allow_non_registered:
                logger.debug(f"Blacklisting un-registered hotkey {caller_hotkey}")
                return True

        uid = self.metagraph.hotkeys.index(caller_hotkey)

        if self.config.blacklist.force_validator_permit:
            if not self.metagraph.neurons[uid].validator_permit:
                logger.warning(f"Blacklisting non-validator hotkey {caller_hotkey}")
                return True

        logger.debug(f"Allowing recognized hotkey {caller_hotkey}")
        return False


# This is the main function, which runs the miner.
if __name__ == "__main__":
    with Miner() as miner:
        logger.info(
            "Miner builder policy=%s role=%s guide_variants=%d primary_cas_share=%.2f artifact_root=%s",
            miner.builder_policy.policy_id,
            miner.builder_policy.role,
            miner.builder_policy.guide_variants_per_target,
            miner.builder_policy.primary_cas_share,
            miner.artifact_root,
        )
        while True:
            logger.info(f"Miner running... {time.time()}")
            time.sleep(5)
