# The MIT License (MIT)
# Copyright © 2023 GitPhantomman
# Copyright © 2023 Rapiiidooo

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import argparse

# Step 1: Import necessary libraries and modules
import os
import traceback
import typing

import bittensor as bt
import sys
import time

import Miner.allocate as al
import Miner.performance as pf
import Miner.pow as p

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(parent_dir)

import compute


whitelisted_hotkeys_set: set = set()


def get_config():
    global whitelisted_hotkeys_set

    # Step 2: Set up the configuration parser
    # This function initializes the necessary command-line arguments.
    parser = argparse.ArgumentParser()
    # Adds override arguments for network and netuid.
    parser.add_argument("--netuid", type=int, default=1, help="The chain subnet uid.")
    parser.add_argument("--auto_update", default="yes", help="Auto update")
    parser.add_argument(
        "--whitelist.hotkeys",
        type=compute.util.parse_list,
        dest="whitelist_hotkeys",
        help="The list of the whitelist hotkeys in the following format: \"['hotkey_x', '...']\"",
        default=[],
    )
    parser.add_argument(
        "--hashcat.path",
        type=str,
        dest="hashcat_path",
        help="The path of the hashcat binary.",
        default=compute.default_hashcat_location,
    )
    parser.add_argument(
        "--hashcat.workload.profile",
        type=str,
        dest="hashcat_workload_profile",
        help="Performance to apply with hashcat profile: 1 Low, 2 Economic, 3 High, 4 Insane. Run `hashcat -h` for more information.",
        default=compute.default_hashcat_workload_profile,
    )
    parser.add_argument(
        "--hashcat.extended.options",
        type=str,
        dest="hashcat_extended_options",
        help="Any extra options you found usefull to append to the hascat runner (I'd perhaps recommend -O). Run `hashcat -h` for more information.",
        default="",
    )
    # Adds subtensor specific arguments i.e. --subtensor.chain_endpoint ... --subtensor.network ...
    bt.subtensor.add_args(parser)
    # Adds logging specific arguments i.e. --logging.debug ..., --logging.trace .. or --logging.logging_dir ...
    bt.logging.add_args(parser)
    # Adds wallet specific arguments i.e. --wallet.name ..., --wallet.hotkey ./. or --wallet.path ...
    bt.wallet.add_args(parser)
    # Adds axon specific arguments i.e. --axon.port ...
    bt.axon.add_args(parser)
    # Activating the parser to read any command-line inputs.
    config = bt.config(parser)

    if config.whitelist_hotkeys:
        for hotkey in config.whitelist_hotkeys:
            whitelisted_hotkeys_set.add(hotkey)

    # Step 3: Set up logging directory
    # Logging captures events for diagnosis or understanding miner's behavior.
    config.full_path = os.path.expanduser(
        "{}/{}/{}/netuid{}/{}".format(
            config.logging.logging_dir,
            config.wallet.name,
            config.wallet.hotkey,
            config.netuid,
            "miner",
        )
    )
    # Ensure the directory for logging exists, else create one.
    if not os.path.exists(config.full_path):
        os.makedirs(config.full_path, exist_ok=True)
    return config


# Main takes the config and starts the miner.
def main(config):
    # Activating Bittensor's logging with the set configurations.
    bt.logging(config=config, logging_dir=config.full_path)
    bt.logging.info(f"Running miner for subnet: {config.netuid} on network: {config.subtensor.chain_endpoint} with config:")

    # This logs the active configuration to the specified logging directory for review.
    # bt.logging.info(config)

    # Step 4: Initialize Bittensor miner objects
    # These classes are vital to interact and function within the Bittensor network.
    bt.logging.info("Setting up bittensor objects.")

    # Wallet holds cryptographic information, ensuring secure transactions and communication.
    wallet = bt.wallet(config=config)
    bt.logging.info(f"Wallet: {wallet}")

    # subtensor manages the blockchain connection, facilitating interaction with the Bittensor blockchain.
    subtensor = bt.subtensor(config=config)
    bt.logging.info(f"Subtensor: {subtensor}")

    # metagraph provides the network's current state, holding state about other participants in a subnet.
    metagraph = subtensor.metagraph(config.netuid)
    bt.logging.info(f"Metagraph: {metagraph}")

    compute.subtensor_utils.is_registered(wallet=wallet, metagraph=metagraph, subtensor=subtensor, entity="miner")

    p.check_cuda_availability()

    hashcat_path = config.hashcat_path
    hashcat_workload_profile = config.hashcat_workload_profile
    hashcat_extended_options = config.hashcat_extended_options

    compute.util.check_hashcat_available(hashcat_path=hashcat_path)

    # Each miner gets a unique identity (UID) in the network for differentiation.
    my_subnet_uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
    bt.logging.info(f"Running miner on uid: {my_subnet_uid}")

    # Step 5: Set up miner functionalities
    # The following functions control the miner's response to incoming requests.
    def base_blacklist(synapse: typing.Union[compute.protocol.PerfInfo, compute.protocol.Allocate, compute.protocol.Challenge]) -> typing.Tuple[bool, str]:
        if synapse.dendrite.hotkey not in metagraph.hotkeys:
            # Ignore requests from unrecognized entities.
            bt.logging.trace(f"Blacklisting unrecognized hotkey {synapse.dendrite.hotkey}")
            return True, "Unrecognized hotkey"

        index = metagraph.hotkeys.index(synapse.dendrite.hotkey)
        stake = metagraph.S[index].item()

        if stake < 1024:
            bt.logging.trace(f"Not enough stake {stake}")
            return True, "Not enough stake!"

        if len(whitelisted_hotkeys_set) > 0 and synapse.dendrite.hotkey not in whitelisted_hotkeys_set:
            return True, "Not whitelisted"

        bt.logging.trace(f"Not Blacklisting recognized hotkey {synapse.dendrite.hotkey}")
        return False, "Hotkey recognized!"

    def base_priority(synapse: typing.Union[compute.protocol.PerfInfo, compute.protocol.Allocate, compute.protocol.Challenge]) -> float:
        caller_uid = metagraph.hotkeys.index(synapse.dendrite.hotkey)  # Get the caller index.
        priority = float(metagraph.S[caller_uid])  # Return the stake as the priority.
        bt.logging.trace(f"Prioritizing {synapse.dendrite.hotkey} with value: ", priority)
        return priority

    # The blacklist function decides if a request should be ignored.
    def blacklist_perfInfo(synapse: compute.protocol.PerfInfo) -> typing.Tuple[bool, str]:
        return base_blacklist(synapse)

    # The priority function determines the order in which requests are handled.
    # More valuable or higher-priority requests are processed before others.
    def priority_perfInfo(synapse: compute.protocol.PerfInfo) -> float:
        return base_priority(synapse) + compute.priority_perfinfo

    # This is the PerfInfo function, which decides the miner's response to a valid, high-priority request.
    def perfInfo(synapse: compute.protocol.PerfInfo) -> compute.protocol.PerfInfo:
        app_data = synapse.perf_input
        synapse.perf_output = pf.get_respond(app_data)
        return synapse

    # The blacklist function decides if a request should be ignored.
    def blacklist_allocate(synapse: compute.protocol.Allocate) -> typing.Tuple[bool, str]:
        return base_blacklist(synapse)

    # The priority function determines the order in which requests are handled.
    # More valuable or higher-priority requests are processed before others.
    def priority_allocate(synapse: compute.protocol.Allocate) -> float:
        return base_priority(synapse) + compute.priority_allocate

    # This is the Allocate function, which decides the miner's response to a valid, high-priority request.
    def allocate(synapse: compute.protocol.Allocate) -> compute.protocol.Allocate:
        timeline = synapse.timeline
        device_requirement = synapse.device_requirement
        checking = synapse.checking

        result = True
        if checking == True:
            result = al.check(timeline, device_requirement)
            synapse.output = result
        else:
            public_key = synapse.public_key
            result = al.register(timeline, device_requirement, public_key)
            synapse.output = result
        return synapse

    # The blacklist function decides if a request should be ignored.
    def blacklist_challenge(synapse: compute.protocol.Challenge) -> typing.Tuple[bool, str]:
        return base_blacklist(synapse)

    # The priority function determines the order in which requests are handled.
    # More valuable or higher-priority requests are processed before others.
    def priority_challenge(synapse: compute.protocol.Challenge) -> float:
        return base_priority(synapse) + compute.priority_challenge

    # This is the Challenge function, which decides the miner's response to a valid, high-priority request.
    def challenge(synapse: compute.protocol.Challenge) -> compute.protocol.Challenge:
        bt.logging.info(f"Received challenge (hash, salt): ({synapse.challenge_hash}, {synapse.challenge_salt})")
        result = p.run_miner_pow(
            _hash=synapse.challenge_hash,
            salt=synapse.challenge_salt,
            mode=synapse.challenge_mode,
            chars=synapse.challenge_chars,
            mask=synapse.challenge_mask,
            hashcat_path=hashcat_path,
            hashcat_workload_profile=hashcat_workload_profile,
            hashcat_extended_options=hashcat_extended_options,
        )
        synapse.output = result
        return synapse

    # Step 6: Build and link miner functions to the axon.
    # The axon handles request processing, allowing validators to send this process requests.
    axon = bt.axon(wallet=wallet, config=config)
    bt.logging.info(f"Axon {axon}")

    # Attach determiners which functions are called when servicing a request.
    bt.logging.info(f"Attaching forward function to axon.")
    axon.attach(
        forward_fn=allocate,
        blacklist_fn=blacklist_allocate,
        priority_fn=priority_allocate,
    ).attach(
        forward_fn=challenge,
        blacklist_fn=blacklist_challenge,
        priority_fn=priority_challenge,
    ).attach(
        forward_fn=perfInfo,
        blacklist_fn=blacklist_perfInfo,
        priority_fn=priority_perfInfo,
    )

    # Serve passes the axon information to the network + netuid we are hosting on.
    # This will auto-update if the axon port of external ip have changed.
    bt.logging.info(f"Serving axon {perfInfo, allocate, challenge} on network: {config.subtensor.chain_endpoint} with netuid: {config.netuid}")
    axon.serve(netuid=config.netuid, subtensor=subtensor)

    # Start  starts the miner's axon, making it active on the network.
    bt.logging.info(f"Starting axon server on port: {config.axon.port}")
    axon.start()

    # This loop maintains the miner's operations until intentionally stopped.
    bt.logging.info(f"Starting main loop")
    step = 0
    while True:
        try:
            # Periodically update our knowledge of the network graph.
            if step % 5 == 0:
                metagraph = subtensor.metagraph(config.netuid)
                log = (
                    f"Step:{step} | "
                    f"Block:{metagraph.block.item()} | "
                    f"Stake:{metagraph.S[my_subnet_uid]} | "
                    f"Rank:{metagraph.R[my_subnet_uid]} | "
                    f"Trust:{metagraph.T[my_subnet_uid]} | "
                    f"Consensus:{metagraph.C[my_subnet_uid] } | "
                    f"Incentive:{metagraph.I[my_subnet_uid]} | "
                    f"Emission:{metagraph.E[my_subnet_uid]}"
                )
                bt.logging.info(log)
            # Check for auto update
            if step % 30 == 0 and config.auto_update == "yes":
                compute.util.try_update()
            step += 1
            time.sleep(1)

        # If someone intentionally stops the miner, it'll safely terminate operations.
        except KeyboardInterrupt:
            axon.stop()
            bt.logging.success("Miner killed by keyboard interrupt.")
            break
        # In case of unforeseen errors, the miner will log the error and continue operations.
        except Exception as e:
            bt.logging.error(traceback.format_exc())
            continue


# This is the main function, which runs the miner.
if __name__ == "__main__":
    main(get_config())
