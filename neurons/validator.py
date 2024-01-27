# The MIT License (MIT)
# Copyright © 2023 Crazydevlegend
# Copyright © 2023 Rapiiidooo
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.
#
# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import ast
import asyncio
import os
import random
import threading
import traceback
from typing import Dict, Tuple, List

import bittensor as bt
import math
import time
import torch
from cryptography.fernet import Fernet
from torch._C._te import Tensor

import Validator.app_generator as ag
from Validator.pow import run_validator_pow, gen_hash
from compute import (
    pow_min_difficulty,
    pow_timeout,
    SUSPECTED_EXPLOITERS_HOTKEYS,
    SUSPECTED_EXPLOITERS_COLDKEYS,
    __version_as_int__,
    weights_rate_limit,
    validator_whitelist_miners_threshold,
)
from compute.axon import ComputeSubnetSubtensor
from compute.protocol import Challenge, Specs
from compute.utils.db import ComputeDb
from compute.utils.math import percent, force_to_float_or_default
from compute.utils.parser import ComputeArgPaser
from compute.utils.subtensor import is_registered, get_current_block
from compute.utils.version import try_update, get_local_version, version2number, get_remote_version
from neurons.Validator.calculate_pow_score import calc_score
from neurons.Validator.database.allocate import update_miner_details
from neurons.Validator.database.challenge import update_challenge_details, select_challenge_stats
from neurons.Validator.database.miner import select_miners, purge_miner_entries, update_miners


class Validator:
    blocks_done: set = set()
    pow_requests: dict = {}
    pow_responses: dict = {}
    pow_benchmark: dict = {}
    new_pow_benchmark: dict = {}
    pow_benchmark_success: dict = {}

    total_current_miners: int = 0

    scores: Tensor
    stats: dict

    validator_subnet_uid: int

    _queryable_uids: Dict[int, bt.AxonInfo]

    @property
    def wallet(self):
        return self._wallet

    @property
    def subtensor(self):
        return self._subtensor

    @property
    def dendrite(self):
        return self._dendrite

    @property
    def metagraph(self):
        return self._metagraph

    @property
    def queryable_uids(self):
        return [uid for uid in self._queryable_uids.keys()]

    @property
    def queryable_axons(self):
        return [axon for axon in self._queryable_uids.values()]

    @property
    def queryable_hotkeys(self):
        return [axon.hotkey for axon in self._queryable_uids.values()]

    @property
    def current_block(self):
        return get_current_block(subtensor=self.subtensor)

    @property
    def miners_items_to_set(self):
        return set((uid, hotkey) for uid, hotkey in self.miners.items()) if self.miners else None

    def __init__(self):
        # Step 1: Parse the bittensor and compute subnet config
        self.config = self.init_config()

        # Set up logging with the provided configuration and directory.
        bt.logging(config=self.config, logging_dir=self.config.full_path)
        bt.logging.info(f"Running validator for subnet: {self.config.netuid} on network: {self.config.subtensor.chain_endpoint} with config:")
        # Log the configuration for reference.
        bt.logging.info(self.config)

        # Step 2: Build Bittensor validator objects
        # These are core Bittensor classes to interact with the network.
        bt.logging.info("Setting up bittensor objects.")

        # The wallet holds the cryptographic key pairs for the validator.
        self._wallet = bt.wallet(config=self.config)
        bt.logging.info(f"Wallet: {self.wallet}")

        # The subtensor is our connection to the Bittensor blockchain.
        self._subtensor = ComputeSubnetSubtensor(config=self.config)
        bt.logging.info(f"Subtensor: {self.subtensor}")

        # Dendrite is the RPC client; it lets us send messages to other nodes (axons) in the network.
        self._dendrite = bt.dendrite(wallet=self.wallet)
        bt.logging.info(f"Dendrite: {self.dendrite}")

        # The metagraph holds the state of the network, letting us know about other miners.
        self._metagraph = self.subtensor.metagraph(self.config.netuid)
        bt.logging.info(f"Metagraph: {self.metagraph}")

        # Set blacklist and whitelist arrays
        self.blacklist_hotkeys = {hotkey for hotkey in self.config.blacklist_hotkeys}
        self.blacklist_coldkeys = {coldkey for coldkey in self.config.blacklist_coldkeys}
        self.whitelist_hotkeys = {hotkey for hotkey in self.config.whitelist_hotkeys}
        self.whitelist_coldkeys = {coldkey for coldkey in self.config.whitelist_coldkeys}

        self.exploiters_hotkeys = {hotkey for hotkey in SUSPECTED_EXPLOITERS_HOTKEYS} if self.config.blacklist_exploiters else {}
        self.exploiters_coldkeys = {coldkey for coldkey in SUSPECTED_EXPLOITERS_COLDKEYS} if self.config.blacklist_exploiters else {}

        # Set custom validator arguments
        self.validator_challenge_batch_size = self.config.validator_challenge_batch_size
        self.validator_perform_hardware_query = self.config.validator_perform_hardware_query

        # Initialize the local db
        self.db = ComputeDb()
        self.miners: dict = select_miners(self.db)

        # Step 4: Set up initial scoring weights for validation
        bt.logging.info("Building validation weights.")
        self.uids: list = self.metagraph.uids.tolist()
        self.last_uids: list = self.uids.copy()
        self.init_scores()
        self.sync_status()

        self.last_updated_block = self.current_block - (self.current_block % 100)

        # Init the thread.
        self.lock = threading.Lock()
        self.threads: List[threading.Thread] = []

    @staticmethod
    def init_config():
        """
        This function is responsible for setting up and parsing command-line arguments.
        :return: config
        """
        parser = ComputeArgPaser(description="This script aims to help validators with the compute subnet.")
        config = parser.config

        # Step 3: Set up logging directory
        # Logging is crucial for monitoring and debugging purposes.
        config.full_path = os.path.expanduser(
            "{}/{}/{}/netuid{}/{}".format(
                config.logging.logging_dir,
                config.wallet.name,
                config.wallet.hotkey,
                config.netuid,
                "validator",
            )
        )
        # Ensure the logging directory exists.
        if not os.path.exists(config.full_path):
            os.makedirs(config.full_path, exist_ok=True)

        # Return the parsed config.
        return config

    def init_prometheus(self, force_update: bool = False):
        """
        Register the prometheus information on metagraph.
        :return: bool
        """
        bt.logging.info("Extrinsic prometheus information on metagraph.")
        success = self.subtensor.serve_prometheus(
            wallet=self.wallet,
            port=bt.defaults.axon.port,
            netuid=self.config.netuid,
            force_update=force_update,
        )
        if success:
            bt.logging.success(prefix="Prometheus served", sufix=f"<blue>Current version: {get_local_version()}</blue>")
        else:
            bt.logging.error("Prometheus initialization failed")
        return success

    def init_scores(self):
        self.scores = torch.zeros(len(self.uids), dtype=torch.float32)
        # Set the weights of validators to zero.
        self.scores = self.scores * (self.metagraph.total_stake < 1.024e3)
        # Set the weight to zero for all nodes without assigned IP addresses.
        self.scores = self.scores * torch.Tensor(self.get_valid_tensors(metagraph=self.metagraph))
        bt.logging.info(f"🔢 Initialized scores : {self.scores.tolist()}")
        self.sync_scores()

    @staticmethod
    def pretty_print_dict_values(items: dict):
        for key, values in items.items():
            log = f"uid: {key}"

            for values_key, values_values in values.items():
                if values_key == "ss58_address":
                    values_values = values_values[:8] + (values_values[8:] and "...")
                try:
                    values_values = f"{float(values_values):.2f}"
                except Exception:
                    pass
                log += f" | {values_key}: {values_values}"

            bt.logging.trace(log)

    def init_local(self):
        bt.logging.info(f"🔄 Syncing metagraph with subtensor.")
        self._metagraph = self.subtensor.metagraph(self.config.netuid)
        self.uids = self.metagraph.uids.tolist()

    def sync_scores(self):
        # Fetch scoring stats
        self.stats = select_challenge_stats(self.db)

        self.pretty_print_dict_values(self.stats)

        # Calculate score
        for uid in self.uids:
            try:
                hotkey = self.stats[uid].get("ss58_address")
                score = calc_score(self.stats[uid], hotkey=hotkey)
            except (ValueError, KeyError):
                score = 0

            self.scores[uid] = score

        bt.logging.info(f"🔢 Synced scores : {self.scores.tolist()}")

    def sync_local(self):
        """
        Resync our local state with the latest state from the blockchain.
        Sync scores with metagraph.
        Get the current uids of all miners in the network.
        """
        self.metagraph.sync(subtensor=self.subtensor)
        self.uids = self.metagraph.uids.tolist()

    def sync_status(self):
        # Check if the validator is still registered
        self.validator_subnet_uid = is_registered(
            wallet=self.wallet,
            metagraph=self.metagraph,
            subtensor=self.subtensor,
            entity="validator",
        )

        # Check for auto update
        if self.config.auto_update:
            try_update()

        # Check if the validator has the prometheus info updated
        subnet_prometheus_version = self.metagraph.neurons[self.validator_subnet_uid].prometheus_info.version
        current_version = __version_as_int__
        if subnet_prometheus_version != current_version:
            self.init_prometheus(force_update=True)

    def calc_difficulty(self, uid):
        difficulty = pow_min_difficulty
        try:
            stat = self.stats[uid]
            current_difficulty = math.ceil(force_to_float_or_default(stat.get("last_20_difficulty_avg"), default=pow_min_difficulty))
            last_20_challenge_failed = force_to_float_or_default(stat.get("last_20_challenge_failed"))
            challenge_successes = force_to_float_or_default(stat.get("challenge_successes"))
            if challenge_successes >= 20:
                difficulty = (
                    current_difficulty + 1
                    if last_20_challenge_failed == 0
                    else current_difficulty - 1
                    if last_20_challenge_failed >= 2
                    else current_difficulty
                )
        except KeyError:
            pass
        except Exception as e:
            bt.logging.error(f"{e} => difficulty minimal: {pow_min_difficulty} attributed for {uid}")
        return difficulty

    async def start(self):
        """The Main Validation Loop"""

        # Step 5: Perform queries to miners, scoring, and weight
        block_next_challenge = 1
        block_next_hardware_info = 1
        block_next_sync_status = 1
        block_next_set_weights = self.current_block + weights_rate_limit

        bt.logging.info("Starting validator loop.")
        while True:
            try:
                self.sync_local()

                if self.current_block not in self.blocks_done:
                    self.blocks_done.add(self.current_block)

                    bt.logging.info(f"Next challenge query : #{block_next_challenge}")
                    if self.validator_perform_hardware_query:
                        bt.logging.info(f"Next specs query: #{block_next_hardware_info}")
                    bt.logging.info(f"Next sync_status call: #{block_next_sync_status}")
                    bt.logging.info(f"Next set_weights call: #{block_next_set_weights}")

                    # Perform pow queries
                    if self.current_block % block_next_challenge == 0:
                        # Next block the validators will challenge again.
                        block_next_challenge = self.current_block + random.randint(40, 80)  # between ~ 8 and 16 minutes

                        # Filter axons with stake and ip address.
                        self._queryable_uids = self.get_queryable()

                        self.pow_requests = {}
                        self.new_pow_benchmark = {}

                        for i in range(0, len(self.uids), self.validator_challenge_batch_size):
                            self.threads = []
                            for _uid in self.uids[i : i + self.validator_challenge_batch_size]:
                                try:
                                    axon = self._queryable_uids[_uid]
                                    difficulty = self.calc_difficulty(_uid)
                                    password, _hash, _salt, mode, chars, mask = run_validator_pow(length=difficulty)
                                    self.pow_requests[_uid] = (password, _hash, _salt, mode, chars, mask, difficulty)
                                    self.threads.append(
                                        threading.Thread(
                                            target=self.execute_pow_request,
                                            args=(_uid, axon, _hash, _salt, mode, chars, mask, difficulty),
                                            name=f"th_execute_pow_request-{_uid}",
                                            daemon=True,
                                        )
                                    )
                                except KeyError:
                                    continue

                        for thread in self.threads:
                            thread.start()

                        for thread in self.threads:
                            thread.join()

                        self.pow_benchmark = self.new_pow_benchmark
                        self.pow_benchmark_success = {k: v for k, v in self.pow_benchmark.items() if v["success"] is True and v["elapsed_time"] < pow_timeout}

                        # Logs benchmarks for the validators
                        if len(self.pow_benchmark_success) > 0:
                            bt.logging.info("🔢 Results success benchmarking:")
                            for uid, benchmark in self.pow_benchmark_success.items():
                                bt.logging.info(f"{uid}: {benchmark}")
                        else:
                            bt.logging.warning("🔢 Results success benchmarking: All miners failed. There must have been a problem.")

                        pow_benchmarks_list = [{**values, "uid": uid} for uid, values in self.pow_benchmark.items()]
                        update_challenge_details(self.db, pow_benchmarks_list)

                        self.sync_scores()

                    if self.current_block % block_next_hardware_info == 0 and self.validator_perform_hardware_query:
                        block_next_hardware_info = self.current_block + 125  # ~ every 25 minutes

                        # # Prepare app_data for benchmarking
                        # # Generate secret key for app
                        secret_key = Fernet.generate_key()
                        cipher_suite = Fernet(secret_key)
                        # # Compile the script and generate an exe.
                        ag.run(secret_key)
                        try:
                            main_dir = os.path.dirname(os.path.abspath(__file__))
                            file_name = os.path.join(main_dir, "Validator/dist/script")
                            # Read the exe file and save it to app_data.
                            with open(file_name, "rb") as file:
                                # Read the entire content of the EXE file
                                app_data = file.read()
                        except Exception as e:
                            bt.logging.error(f"{e}")
                            continue
                        # Query the miners for benchmarking
                        bt.logging.info(f"🆔 Hardware list of uids : {self.queryable_uids}")
                        responses = self.dendrite.query(
                            self.queryable_axons,
                            Specs(specs_input=repr(app_data)),
                            timeout=60,
                        )

                        # Format responses and save them to benchmark_responses
                        hardware_list_responses = []
                        for index, response in enumerate(responses):
                            try:
                                if response:
                                    binary_data = ast.literal_eval(response)  # Convert str to binary data
                                    decoded_data = ast.literal_eval(cipher_suite.decrypt(binary_data).decode())  # Decrypt data and convert it to object
                                    hardware_list_responses.append(decoded_data)
                                else:
                                    hardware_list_responses.append({})
                            except Exception as _:
                                hardware_list_responses.append({})

                        update_miner_details(self.db, self.queryable_hotkeys, hardware_list_responses)
                        bt.logging.info(f"🔢 Hardware list responses : {hardware_list_responses}")

                    if self.current_block % block_next_sync_status == 0:
                        block_next_sync_status = self.current_block + 25  # ~ every 5 minutes
                        self.sync_status()

                    # Periodically update the weights on the Bittensor blockchain, ~ every 20 minutes
                    if self.current_block - self.last_updated_block > weights_rate_limit:
                        block_next_set_weights = self.current_block + weights_rate_limit
                        self.sync_scores()
                        self.set_weights()
                        self.last_updated_block = self.current_block
                        self.blocks_done.clear()
                        self.blocks_done.add(self.current_block)

                bt.logging.info(
                    (
                        f"Block:{self.current_block} | "
                        f"Stake:{self.metagraph.S[self.validator_subnet_uid]} | "
                        f"Rank:{self.metagraph.R[self.validator_subnet_uid]} | "
                        f"vTrust:{self.metagraph.validator_trust[self.validator_subnet_uid]} | "
                        f"Emission:{self.metagraph.E[self.validator_subnet_uid]}"
                    )
                )
                time.sleep(1)

            # If we encounter an unexpected error, log it for debugging.
            except RuntimeError as e:
                bt.logging.error(e)
                traceback.print_exc()

            # If the user interrupts the program, gracefully exit.
            except KeyboardInterrupt:
                self.db.close()
                bt.logging.success("Keyboard interrupt detected. Exiting validator.")
                exit()

    def set_weights(self):
        weights: torch.FloatTensor = torch.nn.functional.normalize(self.scores, p=1.0, dim=0).float()
        bt.logging.info(f"🏋️ Weight of miners : {weights.tolist()}")
        # This is a crucial step that updates the incentive mechanism on the Bittensor blockchain.
        # Miners with higher scores (or weights) receive a larger share of TAO rewards on this subnet.
        result = self.subtensor.set_weights(
            netuid=self.config.netuid,  # Subnet to set weights on.
            wallet=self.wallet,  # Wallet to sign set weights using hotkey.
            uids=self.uids,  # Uids of the miners to set weights for.
            weights=weights,  # Weights to set for the miners.
            version_key=__version_as_int__,
            wait_for_inclusion=False,
        )
        if result:
            bt.logging.success("Successfully set weights.")
        else:
            bt.logging.error("Failed to set weights.")

    def sync_miners_info(self, queryable_tuple_uids_axons: List[Tuple[int, bt.AxonInfo]]):
        if queryable_tuple_uids_axons:
            for uid, axon in queryable_tuple_uids_axons:
                if self.miners_items_to_set and (uid, axon.hotkey) not in self.miners_items_to_set:
                    try:
                        bt.logging.info(f"Miner {uid}-{self.miners[uid]} has been deregistered. Clean up old entries.")
                        purge_miner_entries(self.db, uid, self.miners[uid])
                    except KeyError:
                        pass
                    bt.logging.info(f"Setting up new miner {uid}-{axon.hotkey}.")
                    update_miners(self.db, [(uid, axon.hotkey)]),
                    self.miners[uid] = axon.hotkey
        else:
            bt.logging.warning(f"No queryable miners.")

    @staticmethod
    def filter_axons(queryable_tuple_uids_axons: List[Tuple[int, bt.AxonInfo]]):
        """Filter the axons with uids_list, remove those with the same IP address."""
        # Set to keep track of unique identifiers
        valid_ip_addresses = set()

        # List to store filtered axons
        dict_filtered_axons = {}
        for uid, axon in queryable_tuple_uids_axons:
            ip_address = axon.ip

            if ip_address not in valid_ip_addresses:
                valid_ip_addresses.add(ip_address)
                dict_filtered_axons[uid] = axon

        return dict_filtered_axons

    def filter_axon_version(self, dict_filtered_axons: dict):
        # Get the minimal miner version
        latest_version = version2number(get_remote_version(pattern="__minimal_miner_version__"))
        if percent(len(dict_filtered_axons), self.total_current_miners) <= validator_whitelist_miners_threshold:
            bt.logging.info(f"More than {100 - validator_whitelist_miners_threshold}% miners are currently using an old version.")
            return dict_filtered_axons

        dict_filtered_axons_version = {}
        for uid, axon in dict_filtered_axons.items():
            if latest_version and axon.version >= latest_version:
                dict_filtered_axons_version[uid] = axon
        return dict_filtered_axons_version

    def is_blacklisted(self, neuron: bt.NeuronInfoLite):
        coldkey = neuron.coldkey
        hotkey = neuron.hotkey

        # Blacklist coldkeys that are blacklisted by user
        if coldkey in self.blacklist_coldkeys:
            bt.logging.trace(f"Blacklisted recognized coldkey {coldkey} - with hotkey: {hotkey}")
            return True

        # Blacklist coldkeys that are blacklisted by user or by set of hotkeys
        if hotkey in self.blacklist_hotkeys:
            bt.logging.trace(f"Blacklisted recognized hotkey {hotkey}")
            # Add the coldkey attached to this hotkey in the blacklisted coldkeys
            self.blacklist_hotkeys.add(coldkey)
            return True

        # Blacklist coldkeys that are exploiters
        if coldkey in self.exploiters_coldkeys:
            bt.logging.trace(f"Blacklisted exploiter coldkey {coldkey} - with hotkey: {hotkey}")
            return True

        # Blacklist hotkeys that are exploiters
        if hotkey in self.exploiters_hotkeys:
            bt.logging.trace(f"Blacklisted exploiter hotkey {hotkey}")
            # Add the coldkey attached to this hotkey in the blacklisted coldkeys
            self.exploiters_hotkeys.add(coldkey)
            return True

        return False

    def get_valid_queryable(self):
        valid_queryable = []
        for uid in self.uids:
            neuron: bt.NeuronInfoLite = self.metagraph.neurons[uid]
            axon = self.metagraph.axons[uid]

            if neuron.axon_info.ip != "0.0.0.0" and self.metagraph.total_stake[uid] < 1.024e3 and not self.is_blacklisted(neuron=neuron):
                valid_queryable.append((uid, axon))

        return valid_queryable

    def get_valid_tensors(self, metagraph):
        tensors = []
        for uid in metagraph.uids:
            neuron = metagraph.neurons[uid]

            if neuron.axon_info.ip != "0.0.0.0" and not self.is_blacklisted(neuron=neuron):
                tensors.append(True)
            else:
                tensors.append(False)

        return tensors

    def get_queryable(self):
        queryable = self.get_valid_queryable()

        # Execute a cleanup of the stats and miner information if the miner has been dereg
        self.sync_miners_info(queryable)

        dict_filtered_axons = self.filter_axons(queryable_tuple_uids_axons=queryable)
        dict_filtered_axons = self.filter_axon_version(dict_filtered_axons=dict_filtered_axons)
        return dict_filtered_axons

    def execute_pow_request(self, uid, axon: bt.AxonInfo, _hash, _salt, mode, chars, mask, difficulty):
        dendrite = bt.dendrite(wallet=self.wallet)
        start_time = time.time()
        response = dendrite.query(
            axon,
            Challenge(
                challenge_hash=_hash,
                challenge_salt=_salt,
                challenge_mode=mode,
                challenge_chars=chars,
                challenge_mask=mask,
                challenge_difficulty=difficulty,
            ),
            timeout=pow_timeout,
        )
        elapsed_time = time.time() - start_time
        response_password = response.get("password", "")
        hashed_response = gen_hash(response_password, _salt)[0] if response_password else ""
        success = True if _hash == hashed_response else False
        result_data = {
            "ss58_address": axon.hotkey,
            "success": success,
            "elapsed_time": elapsed_time,
            "difficulty": difficulty,
        }
        with self.lock:
            self.pow_responses[uid] = response
            self.new_pow_benchmark[uid] = result_data


def main():
    """
    Main function to run the neuron.

    This function initializes and runs the neuron. It handles the main loop, state management, and interaction
    with the Bittensor network.
    """
    validator = Validator()
    asyncio.run(validator.start())


if __name__ == "__main__":
    main()
