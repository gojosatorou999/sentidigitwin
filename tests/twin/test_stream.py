"""SSE broker tests (Phase 7). No real HTTP connection -- exercises
twin.stream's pub/sub directly, matching how twin/routes.py's
/api/twin/stream route and twin/jobs.py's compute job use it.
"""

import queue

from twin import stream as twin_stream


class TestPublishSubscribe:
    def setup_method(self):
        # each test gets a clean subscriber table
        twin_stream._subscribers.clear()

    def test_subscriber_receives_matching_city_event(self):
        sub_id = twin_stream.subscribe(city="hyderabad")
        twin_stream.publish("state_update", city="hyderabad", changed_cells=["a"])

        _, _, q = twin_stream._subscribers[sub_id]
        event = q.get_nowait()
        assert event["type"] == "state_update"
        assert event["changed_cells"] == ["a"]

    def test_subscriber_does_not_receive_other_citys_event(self):
        sub_id = twin_stream.subscribe(city="hyderabad")
        twin_stream.publish("state_update", city="bengaluru", changed_cells=["b"])

        _, _, q = twin_stream._subscribers[sub_id]
        assert q.empty()

    def test_unfiltered_subscriber_receives_every_city(self):
        sub_id = twin_stream.subscribe(city=None)
        twin_stream.publish("state_update", city="hyderabad", changed_cells=["a"])
        twin_stream.publish("state_update", city="bengaluru", changed_cells=["b"])

        _, _, q = twin_stream._subscribers[sub_id]
        assert q.qsize() == 2

    def test_unsubscribe_removes_the_subscriber(self):
        sub_id = twin_stream.subscribe(city="hyderabad")
        twin_stream.unsubscribe(sub_id)
        assert sub_id not in twin_stream._subscribers

    def test_publish_never_raises_on_a_full_queue(self):
        sub_id = twin_stream.subscribe(city="hyderabad")
        _, _, q = twin_stream._subscribers[sub_id]
        for _ in range(twin_stream.QUEUE_MAXSIZE):
            q.put_nowait({"type": "filler"})

        try:
            twin_stream.publish("state_update", city="hyderabad")  # queue is full
        except queue.Full:
            raise AssertionError("publish() must never raise on a full subscriber queue")

    def test_publish_with_no_subscribers_is_a_noop(self):
        twin_stream.publish("state_update", city="hyderabad")  # must not raise

    def test_subscriber_count(self):
        assert twin_stream.subscriber_count() == 0
        twin_stream.subscribe()
        twin_stream.subscribe()
        assert twin_stream.subscriber_count() == 2


class TestSSEStreamGenerator:
    def setup_method(self):
        twin_stream._subscribers.clear()

    def test_yields_connected_comment_then_stops_at_max_duration(self):
        # The duration cap must be responsive down to well below
        # HEARTBEAT_INTERVAL_S (15s) -- a caller passing a small
        # max_duration_s must not block for a full heartbeat past it.
        gen = twin_stream.sse_stream(city="hyderabad", max_duration_s=0.05)
        chunks = list(gen)  # exhausts once the generator hits its cap and returns

        assert chunks[0] == ": connected\n\n"
        assert len(chunks) <= 4  # connected + a couple heartbeats, not a hang

    def test_delivers_a_published_event_before_the_duration_cap(self):
        gen = twin_stream.sse_stream(city="hyderabad", max_duration_s=2.0)
        assert next(gen) == ": connected\n\n"

        twin_stream.publish("state_update", city="hyderabad", changed_cells=["x"])
        chunk = next(gen)

        assert "state_update" in chunk
        assert '"x"' in chunk
        gen.close()

    def test_cleans_up_subscriber_on_generator_close(self):
        gen = twin_stream.sse_stream(city="hyderabad", max_duration_s=0.01)
        next(gen)  # advance past ": connected" to actually subscribe
        assert twin_stream.subscriber_count() == 1
        gen.close()
        assert twin_stream.subscriber_count() == 0
