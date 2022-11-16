# Copyright 2022 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import TYPE_CHECKING

import attr

from synapse.replication.tcp.streams import Stream
from synapse.replication.tcp.streams._base import current_token_without_instance
from synapse.types import RoomID

if TYPE_CHECKING:
    from synapse.server import HomeServer


class UnPartialStatedRoomStream(Stream):
    """
    Stream to notify about rooms becoming un-partial-stated;
    that is, when the background sync finishes such that we now have full state for
    the room.
    """

    @attr.s(slots=True, frozen=True, auto_attribs=True)
    class UnPartialStatedRoomStreamRow:
        room_id: RoomID

    NAME = "un_partial_stated_room"
    ROW_TYPE = UnPartialStatedRoomStreamRow

    def __init__(self, hs: "HomeServer"):
        store = hs.get_datastores().main
        super().__init__(
            hs.get_instance_name(),
            # TODO(multiple writers): we need to account for instance names
            current_token_without_instance(store.get_un_partial_stated_rooms_token),
            store.get_un_partial_stated_rooms_from_stream,
        )
