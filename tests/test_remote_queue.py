"""Following a remote player's queue: a stand-in phone plays its own play queue until tamp
hands it one, and takes a moment to switch, as Plexamp does."""

import asyncio
import threading
import time
from types import SimpleNamespace

import plexapi.playqueue
import pytest

from tamp.companion import Timeline
from tamp.models import Device, Track
from tamp.output import RemoteOutput
from tamp.player import Player
from tamp.queue import Queue


def T(key: str) -> Track:
    return Track(id=key, title=key, artist="a", album="b", duration_ms=200_000)


class Lib:
    def track_from(self, item) -> Track:
        return T(str(item.ratingKey))

    def track(self, key: str) -> Track:
        return T(key)


class Phone:
    """Plexamp on a phone, over Companion: its timeline, and the commands tamp sends."""

    def __init__(self, queues: dict[str, list[str]], pq: str = "1", switch_after: float = 0.0) -> None:
        self.queues = queues
        self.pq, self.key, self.version = pq, queues[pq][0], 1
        self.switch_after = switch_after  # how long it takes to move to a queue tamp hands it
        self.handoffs = 0
        self.commands: list[tuple] = []
        self.adds: list[tuple[str, bool]] = []  # what reached its play queue on the server
        self.errors: list[str] = []  # what tamp told you about it
        self._next = 100

    def timeline(self, wait: bool = False) -> Timeline:
        return Timeline(
            state="playing",
            time_ms=1000,
            duration_ms=200_000,
            rating_key=self.key,
            play_queue_id=self.pq,
            volume=100,
            play_queue_version=self.version,
        )

    def play_queue(self, server, rating_keys: list[str], start_key: str, shuffle: bool = False) -> None:
        self.handoffs += 1
        new, self._next = str(self._next), self._next + 1
        self.queues[new] = list(rating_keys)

        def switch() -> None:
            self.pq, self.key, self.version = new, start_key, 1

        if self.switch_after:
            timer = threading.Timer(self.switch_after, switch)
            timer.daemon = True  # one that never fires mustn't hold up the test run's exit
            timer.start()
        else:
            switch()

    def skip_to(self, rating_key: str) -> None:
        self.commands.append(("skip_to", rating_key))
        self.key = rating_key

    def __getattr__(self, name: str):
        def record(*args, **kwargs) -> None:
            self.commands.append((name, *args, *kwargs.items()))

        return record


@pytest.fixture
def phone_queues(monkeypatch):
    """PlayQueue.get reads the stand-in phone's queues, as the server would."""
    phones: list[Phone] = []

    def get(cls, server, play_queue_id, own=False, window=50, **_kw):
        phone = phones[-1]
        keys = phone.queues[str(play_queue_id)]
        items = [
            SimpleNamespace(type="track", ratingKey=k, playQueueItemID=int(play_queue_id) * 1000 + n)
            for n, k in enumerate(keys)
        ]
        selected = keys.index(phone.key) if phone.key in keys else 0

        def add_item(item, playNext=False, refresh=True) -> None:
            phone.adds.append((str(item.ratingKey), playNext))
            keys.insert(keys.index(phone.key) + 1, str(item.ratingKey))  # simplified Up Next
            phone.version += 1

        return SimpleNamespace(
            items=items, playQueueSelectedItemID=items[selected].playQueueItemID, addItem=add_item
        )

    monkeypatch.setattr(plexapi.playqueue.PlayQueue, "get", classmethod(get))
    return phones


SERVER = SimpleNamespace(fetchItem=lambda key: SimpleNamespace(ratingKey=key.rsplit("/", 1)[-1]))


async def _following(phone: Phone, phones: list[Phone], handoff_s: float = 5.0):
    phones.append(phone)
    out = RemoteOutput(Device("phone", "iPhone"), server=SERVER, library=Lib(), companion=phone)
    out.POLL_S, out.HANDOFF_S = 0.02, handoff_s
    player = Player(Lib(), on_update=lambda s, q: None, on_error=phone.errors.append, report=False)
    adopted: list[list[str]] = []

    def adopt(queue) -> None:
        adopted.append([t.id for t in queue.tracks])
        player.adopt_queue(queue)

    out.on_change, out.on_queue = player.changed, adopt
    await player.use(out)
    return out, player, adopted


def ids(player: Player) -> list[str]:
    return [t.id for t in player.queue.tracks]


async def test_a_queue_started_on_the_phone_is_shown(phone_queues) -> None:
    phone = Phone({"1": ["p1", "p2", "p3"]})
    out, player, adopted = await _following(phone, phone_queues)
    try:
        await asyncio.sleep(0.15)
        assert ids(player) == ["p1", "p2", "p3"] and adopted == [["p1", "p2", "p3"]]
        phone.queues["1"].append("p4")  # something added on the phone bumps the version
        phone.version += 1
        await asyncio.sleep(0.15)
        assert ids(player) == ["p1", "p2", "p3", "p4"]
    finally:
        await out.close()


async def test_the_phones_old_queue_is_not_adopted_while_it_switches_to_ours(phone_queues) -> None:
    phone = Phone({"1": ["p1", "p2", "p3"]}, switch_after=0.3)
    out, player, adopted = await _following(phone, phone_queues)
    try:
        await asyncio.sleep(0.15)
        assert ids(player) == ["p1", "p2", "p3"]
        adopted.clear()
        await player.play([T("t1"), T("t2")], 0)
        await asyncio.sleep(0.6)  # it showed the old queue for a while, then switched
        assert phone.pq != "1" and phone.key == "t1"
        assert adopted == []  # neither the old queue nor ours came back as "the phone's"
        assert ids(player) == ["t1", "t2"] and player.state.index == 0
    finally:
        await out.close()


async def test_playing_right_after_switching_to_the_phone_keeps_ours(phone_queues) -> None:
    phone = Phone({"1": ["p1", "p2", "p3"]}, switch_after=0.3)
    phone_queues.append(phone)
    out = RemoteOutput(Device("phone", "iPhone"), server=None, library=Lib(), companion=phone)
    out.POLL_S = 0.02
    player = Player(Lib(), on_update=lambda s, q: None, report=False)
    adopted: list = []
    out.on_change, out.on_queue = player.changed, adopted.append
    try:
        await player.use(out)
        await player.play([T("t1"), T("t2")], 0)  # before a single poll saw the phone's queue
        await asyncio.sleep(0.6)
        assert ids(player) == ["t1", "t2"] and adopted == []  # ours, known as ours
    finally:
        await out.close()


async def test_a_hand_off_that_never_lands_goes_back_to_following(phone_queues) -> None:
    phone = Phone({"1": ["p1", "p2", "p3"]}, switch_after=60)  # it never gets there
    out, player, _ = await _following(phone, phone_queues, handoff_s=0.2)
    try:
        await asyncio.sleep(0.1)
        await player.play([T("t1"), T("t2")], 0)
        await asyncio.sleep(0.5)
        assert ids(player) == ["p1", "p2", "p3"]  # what the phone really plays
    finally:
        await out.close()


def test_which_play_queues_are_fetched() -> None:
    out = RemoteOutput(Device("phone", "Phone"), server=None, library=None)
    assert out._wants_queue("10", 1, "y")  # a queue started on the phone
    out.queue = Queue(tracks=(T("x"), T("y"), T("z")), current=1)
    assert not out._wants_queue("10", 1, "z")  # it moved on inside that queue: nothing new
    assert out._wants_queue("10", 2, "z")  # something was added on the phone

    out._handoff = ("10", time.monotonic() + 60)  # tamp handed it a queue (load)
    assert not out._wants_queue("10", 2, "z")  # still the old one: the hand-off is on its way
    assert not out._wants_queue("11", 1, "q")  # the new one is ours
    assert not out._wants_queue("11", 2, "q")  # and our own additions bump its version


# ── editing the phone's queue from tamp ──
async def _on_phone(phone_queues) -> tuple[RemoteOutput, Player, Phone]:
    phone = Phone({"1": ["p1", "p2", "p3", "p4"]})
    out, player, _ = await _following(phone, phone_queues)
    await asyncio.sleep(0.1)
    assert ids(player) == ["p1", "p2", "p3", "p4"]
    return out, player, phone


async def test_adding_a_track_already_queued_still_reaches_the_phone(phone_queues) -> None:
    out, player, phone = await _on_phone(phone_queues)
    try:
        assert await player.add_up_next([T("p3")]), phone.errors
        assert phone.adds == [("p3", False)] and ("refresh_play_queue", "1") in phone.commands
        assert await player.play_next([T("x"), T("y")])
        assert phone.adds[1:] == [("y", True), ("x", True)]  # each to the front: x ends up first
        await asyncio.sleep(0.1)
        assert ids(player)[:3] == ["p1", "x", "y"] and phone.errors == []
    finally:
        await out.close()


async def test_choosing_a_queue_row_skips_to_it_without_replacing_the_queue(phone_queues) -> None:
    out, player, phone = await _on_phone(phone_queues)
    try:
        await player.jump(2)
        assert phone.commands == [("skip_to", "p3")] and phone.handoffs == 0
        await asyncio.sleep(0.1)
        assert ids(player) == ["p1", "p2", "p3", "p4"] and player.queue.current == 2
    finally:
        await out.close()


async def test_a_jump_past_what_tamp_handed_the_phone_hands_it_more(phone_queues) -> None:
    from tamp.output import REMOTE_WINDOW

    phone = Phone({"1": ["p1"]})
    out, player, _ = await _following(phone, phone_queues)
    try:
        await player.play([T(str(n)) for n in range(REMOTE_WINDOW * 2)], 0)
        await asyncio.sleep(0.1)
        await player.jump(5)  # inside the window it has: skip
        assert phone.commands == [("skip_to", "5")] and phone.handoffs == 1
        await player.jump(REMOTE_WINDOW + 50)  # beyond it: tamp's own queue, so hand over a new window
        assert phone.handoffs == 2 and phone.key == str(REMOTE_WINDOW + 50)
    finally:
        await out.close()


@pytest.mark.parametrize(
    ("edit", "what"),
    [
        (lambda p: p.remove(2), "Removing tracks"),
        (lambda p: p.remove(0), "Removing tracks"),  # the playing one
        (lambda p: p.move(3, 1), "Moving tracks"),
        (lambda p: p.to_up_next(3), "Moving tracks"),
        (lambda p: p.shuffle(), "Shuffling the queue"),
        (lambda p: p.clear_upcoming(), "Clearing the queue"),
        (lambda p: p.append([T("z")]), "Play last"),
    ],
)
async def test_edits_the_phone_cant_take_yet_say_so_and_change_nothing(phone_queues, edit, what) -> None:
    out, player, phone = await _on_phone(phone_queues)
    try:
        before = player.queue
        await edit(player)
        assert player.queue == before and phone.commands == [] and phone.adds == []
        assert phone.errors == [f"{what} isn't available when playing on iPhone yet"]
    finally:
        await out.close()
