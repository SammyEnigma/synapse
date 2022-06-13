#! /usr/bin/env python
import argparse
import logging
import sys
from collections import defaultdict
from pprint import pformat
from typing import Mapping
from unittest.mock import MagicMock, patch

import dictdiffer
import pydot
import yaml

from twisted.internet import task

from synapse.config._base import RootConfig
from synapse.config.cache import CacheConfig
from synapse.config.database import DatabaseConfig
from synapse.config.homeserver import HomeServerConfig
from synapse.config.workers import WorkerConfig
from synapse.events import EventBase
from synapse.server import HomeServer
from synapse.state import StateResolutionStore
from synapse.storage.databases.main.event_federation import EventFederationWorkerStore
from synapse.storage.databases.main.events_worker import EventsWorkerStore
from synapse.storage.databases.main.room import RoomWorkerStore
from synapse.storage.databases.main.state import StateGroupWorkerStore
from synapse.types import ISynapseReactor, StateMap

logger = logging.getLogger(sys.argv[0])


class Config(RootConfig):
    config_classes = [DatabaseConfig, WorkerConfig, CacheConfig]


def load_config(source: str) -> Config:
    data = yaml.safe_load(source)
    data["worker_name"] = "stateres-debug"

    config = Config()
    config.parse_config_dict(data, "DUMMYPATH", "DUMMYPATH")
    config.key = MagicMock()  # Don't bother creating signing keys
    return config


class DataStore(
    StateGroupWorkerStore,
    EventFederationWorkerStore,
    EventsWorkerStore,
    RoomWorkerStore,
):
    pass


class MockHomeserver(HomeServer):
    DATASTORE_CLASS = DataStore  # type: ignore [assignment]

    def __init__(self, config: HomeServerConfig):
        super(MockHomeserver, self).__init__(
            hostname="stateres-debug",
            config=config,
        )


def node(event: EventBase, **kwargs) -> pydot.Node:
    kwargs.setdefault(
        "label",
        f"{event.event_id}\n{event.type}",
    )
    type_to_shape = {"m.room.member": "oval"}
    if "shape" not in kwargs and event.type in type_to_shape:
        kwargs["shape"] = type_to_shape[event.type]

    q = pydot.quote_if_necessary
    return pydot.Node(q(event.event_id), **kwargs)


async def dump_auth_chains(
    hs: MockHomeserver, state_after_parents: Mapping[str, StateMap[str]]
):
    graph = pydot.Dot(rankdir="BT")
    graph.set_node_defaults(shape="box", style="filled")
    q = pydot.quote_if_necessary

    # Key: event id
    # Value: bitmaps. ith bit is set iff this belongs to the auth chain of the ith
    # starting event.
    seen = defaultdict(int)
    edges = set()

    for i, start in enumerate(state_after_parents):
        bitmask = 1 << i
        # DFS starting at `start`. Entries are [event, auth event index].
        stack = [[start, 0]]
        while stack:
            # Fetch the event we're considering and our progress through its auth events.
            eid, pindex = stack[-1]
            event = await hs.get_datastores().main.get_event(eid, allow_none=True)
            assert event is not None

            # If we've already considered all of its auth events, we can mark this one
            # As having been seen by `start`.
            if pindex >= len(event.auth_event_ids()):
                seen[eid] |= bitmask
                stack.pop()
                continue

            pid = event.auth_event_ids()[pindex]
            edges.add((eid, pid))
            # If we've already marked that `start` can see `pid`, try the next auth event
            if seen.get(pid, 0) & bitmask:
                stack[-1][1] += 1
                continue

            # Otherwise, continue DFS at pid
            stack.append([pid, 0])

    for eid, bitmask in seen.items():
        event = await hs.get_datastores().main.get_event(eid, allow_none=True)
        assert event is not None
        colors = ["gray", "orangered", "lightskyblue", "mediumorchid1"]
        graph.add_node(node(event, fillcolor=colors[bitmask]))
    for eid, pid in edges:
        graph.add_edge(pydot.Edge(q(eid), q(pid)))

    graph.write_raw("auth_chains.dot")
    graph.write_svg("auth_chains.svg")


async def main(reactor: ISynapseReactor, args: argparse.Namespace) -> None:
    config = load_config(args.config_file)
    hs = MockHomeserver(config)
    with patch("synapse.storage.databases.prepare_database"), patch(
        "synapse.storage.database.BackgroundUpdater"
    ), patch("synapse.storage.databases.main.events_worker.MultiWriterIdGenerator"):
        hs.setup()

    # Fetch the event in question.
    event = await hs.get_datastores().main.get_event(args.event_id)
    assert event is not None
    logger.info("event %s has %d parents", event.event_id, len(event.prev_event_ids()))

    state_after_parents = {}
    for i, prev_event_id in enumerate(event.prev_event_ids()):
        # TODO: check this is the state after parents :)
        state_after_parents[
            prev_event_id
        ] = await hs.get_storage_controllers().state.get_state_ids_for_event(
            prev_event_id
        )
        logger.info("parent %d: %s", i, prev_event_id)

    await dump_auth_chains(hs, state_after_parents)
    # return

    result = await hs.get_state_resolution_handler().resolve_events_with_store(
        event.room_id,
        event.room_version.identifier,
        state_after_parents.values(),
        event_map=None,
        state_res_store=StateResolutionStore(hs.get_datastores().main),
    )

    logger.info("State resolved at %s:", event.event_id)
    logger.info(pformat(result))

    logger.info("Stored state at %s:", event.event_id)
    stored_state = await hs.get_storage_controllers().state.get_state_ids_for_event(
        event.event_id
    )
    logger.info(pformat(stored_state))

    logger.info("Diff from stored to resolved:")
    for change in dictdiffer.diff(stored_state, result):
        logger.info(pformat(change))

    if args.debug:
        print(
            f"see state_after_parents[i] for i in range({len(state_after_parents)}"
            " and result",
            file=sys.stderr,
        )
        breakpoint()


parser = argparse.ArgumentParser(
    description="Explain the calculation which resolves state prior before an event"
)
parser.add_argument("event_id", help="the event ID to be resolved")
parser.add_argument(
    "config_file", help="Synapse config file", type=argparse.FileType("r")
)
parser.add_argument("--verbose", "-v", help="Log verbosely", action="store_true")
parser.add_argument(
    "--debug", "-d", help="Enter debugger after state is resolved", action="store_true"
)


if __name__ == "__main__":
    args = parser.parse_args()
    logging.basicConfig(
        format="%(asctime)s %(name)s:%(lineno)d %(levelname)s %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stdout,
    )
    logging.getLogger("synapse.util").setLevel(logging.ERROR)
    logging.getLogger("synapse.storage").setLevel(logging.ERROR)
    task.react(main, [parser.parse_args()])