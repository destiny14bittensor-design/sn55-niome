# The MIT License (MIT)
# Copyright © 2023 Yuma Rao

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

import time
import asyncio
import threading
import argparse
import logging
import traceback

import bittensor as bt
import uvicorn

from fastapi import FastAPI, Request, HTTPException

from niome_subnet.base.neuron import BaseNeuron
from niome_subnet.utils import add_miner_args, fetch_metagraph_with_retry

from typing import Union

logger = logging.getLogger(__name__)

# Validator requests can be delayed by the subnet's sequential broadcast path
# and its proxy. Keep the window bounded by the 300-second submission URL TTL,
# while retaining accepted nonces longer than the whole freshness window.
FORWARD_AUTH_MAX_AGE = 300.0
FORWARD_AUTH_ALLOWED_SKEW = 30.0
FORWARD_AUTH_NONCE_STORE = bt.http_auth.InMemoryNonceStore(retention=360.0)


def verify_forward_request(
    headers,
    body: bytes,
    *,
    self_hotkey_ss58: str,
):
    """Verify a validator request with v11 and legacy-v11 compatibility.

    Bittensor 11 requires receiver-bound signatures by default, but the
    subnet's current validator signs /forward requests without receiver_ss58.
    Keep strict receiver binding whenever the header is present and only use
    the unbound verifier for the legacy request shape. Signature, nonce, and
    request-age validation are still performed by bittensor in both modes.
    """
    has_receiver = any(
        str(name).lower() == "x-bittensor-receiver" for name in headers
    )
    auth_mode = "receiver-bound" if has_receiver else "legacy-unbound"
    caller = bt.http_auth.verify(
        headers,
        body,
        method="POST",
        path="/forward",
        self_hotkey_ss58=self_hotkey_ss58,
        max_age=FORWARD_AUTH_MAX_AGE,
        allowed_skew=FORWARD_AUTH_ALLOWED_SKEW,
        require_receiver=has_receiver,
        nonce_store=FORWARD_AUTH_NONCE_STORE,
    )
    return caller, auth_mode


class BaseMinerNeuron(BaseNeuron):
    """
    Base class for Bittensor miners. Uses a FastAPI HTTP server instead of bt.Axon.
    """

    neuron_type: str = "MinerNeuron"

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        super().add_args(parser)
        add_miner_args(cls, parser)
        parser.add_argument(
            "--axon.port",
            type=int,
            help="Port for the miner HTTP server.",
            default=8091,
        )
        parser.add_argument(
            "--axon.external-ip",
            type=str,
            default=None,
            help="Public IP advertised on-chain; defaults to hostname resolution.",
        )

    def __init__(self, config=None):
        super().__init__(config=config)

        if not self.config.blacklist.force_validator_permit:
            logger.warning(
                "You are allowing non-validators to send requests to your miner. This is a security risk."
            )
        if self.config.blacklist.allow_non_registered:
            logger.warning(
                "You are allowing non-registered entities to send requests to your miner. This is a security risk."
            )

        self.axon_port = getattr(getattr(self.config, "axon", None), "port", 8091)

        # Build the FastAPI app; subclass attaches routes in forward/blacklist/priority
        self.app = FastAPI()
        self._setup_routes()

        # Instantiate runners
        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: Union[threading.Thread, None] = None
        self.lock = asyncio.Lock()

    def _setup_routes(self):
        """Attach the /forward route to the FastAPI app."""
        app = self.app
        miner = self

        @app.post("/forward")
        async def handle_forward(request: Request):
            try:
                headers = dict(request.headers)
                body = await request.body()

                # Verify hotkey-signed request
                try:
                    caller, auth_mode = verify_forward_request(
                        headers,
                        body,
                        self_hotkey_ss58=miner.wallet.hotkey.ss58_address,
                    )
                except bt.http_auth.AuthError as e:
                    client = request.client.host if request.client else "unknown"
                    nonce_age = "unknown"
                    raw_nonce = next(
                        (
                            value
                            for name, value in headers.items()
                            if str(name).lower() == "x-bittensor-nonce"
                        ),
                        None,
                    )
                    if raw_nonce is not None:
                        try:
                            nonce_age = f"{(time.time_ns() - int(raw_nonce)) / 1e9:.3f}s"
                        except (TypeError, ValueError):
                            pass
                    logger.warning(
                        "Rejected /forward authentication from %s (nonce_age=%s): %s",
                        client,
                        nonce_age,
                        e,
                    )
                    raise HTTPException(status_code=401, detail=str(e))

                # Run blacklist check
                if await miner.blacklist(caller.hotkey_ss58):
                    logger.warning(
                        "Rejected blacklisted /forward caller %s",
                        caller.hotkey_ss58,
                    )
                    raise HTTPException(status_code=403, detail="blacklisted")

                logger.info(
                    "Accepted /forward caller %s using %s authentication",
                    caller.hotkey_ss58,
                    auth_mode,
                )
                return await miner.forward(body, caller.hotkey_ss58)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error handling /forward: {e}")
                raise HTTPException(status_code=500, detail=str(e))

    def _serve_axon_on_chain(self):
        """Register this miner's IP:port on chain."""
        try:
            import socket
            ip = self.config.axon.external_ip or socket.gethostbyname(socket.gethostname())
            self.subtensor.execute(
                bt.ServeAxon(
                    netuid=self.config.netuid,
                    ip=ip,
                    port=self.axon_port,
                ),
                self.wallet,
            )
            logger.info(
                f"Served miner axon {ip}:{self.axon_port} on network: {self.config.network} netuid: {self.config.netuid}"
            )
        except Exception as e:
            logger.error(f"Failed to serve axon on chain: {e}")

    def run(self):
        """
        Initiates and manages the main loop for the miner on the Bittensor network.
        """

        # Check that miner is registered on the network.
        self.sync()

        # Register axon on chain.
        self._serve_axon_on_chain()

        logger.info(f"Miner starting at block: {self.block}")

        # Start the FastAPI server in a daemon thread.
        server_config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.axon_port,
            log_level="warning",
        )
        server = uvicorn.Server(server_config)
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        # This loop maintains the miner's operations until intentionally stopped.
        last_sync_block = self.block
        try:
            while not self.should_exit:
                try:
                    while self.block - last_sync_block < self.config.neuron.epoch_length:
                        time.sleep(1)
                        if self.should_exit:
                            break

                    # Miners do not set weights. Refresh on a bounded cadence
                    # rather than spinning when the chain last_update is old.
                    self.resync_metagraph()
                    last_sync_block = self.block
                    self.step += 1

                except Exception as err:
                    logger.warning("Miner step error (will retry): %s", err)
                    logger.debug(traceback.format_exc())
                    time.sleep(12)

        except KeyboardInterrupt:
            logger.info("Miner killed by keyboard interrupt.")
            server.should_exit = True
            exit()
        finally:
            server.should_exit = True

    def run_in_background_thread(self):
        """Starts the miner's operations in a separate background thread."""
        if not self.is_running:
            logger.debug("Starting miner in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True
            logger.debug("Started")

    def stop_run_thread(self):
        """Stops the miner's operations that are running in the background thread."""
        if self.is_running:
            logger.debug("Stopping miner in background thread.")
            self.should_exit = True
            if self.thread is not None:
                self.thread.join(5)
            self.is_running = False
            logger.debug("Stopped")

    def __enter__(self):
        self.run_in_background_thread()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop_run_thread()

    def resync_metagraph(self):
        """Resyncs the metagraph."""
        logger.info("resync_metagraph()")
        self.metagraph = fetch_metagraph_with_retry(self.subtensor, self.netuid, commitments=False)
