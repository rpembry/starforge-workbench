"""In-memory, non-persistent relay for live response excerpts.

An excerpt passes through this module only in process memory. Nothing here
is written to the repository, a file, or any other durable store: a viewer
who is not connected when an excerpt is published never sees it, no backlog
is kept for a viewer who connects later, and a server restart clears
everything. This complements the durable /results reporting path; it never
substitutes for it and a failure here must never affect it.
"""
import asyncio


class ResponsePreviewHub:
    """Fan-out of ephemeral response excerpts to live operator pages."""

    def __init__(self, max_queue=8):
        self._subscribers = set()
        self._max_queue = max_queue

    def subscribe(self):
        queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self._subscribers.discard(queue)

    def publish(self, instruction_id, registered_session_id, outcome, excerpt):
        """Fan an excerpt out to current subscribers only. Returns whether at
        least one subscriber received it; nothing is queued for the future."""
        event = {'instruction_id': instruction_id,
                 'registered_session_id': registered_session_id,
                 'outcome': outcome, 'excerpt': excerpt}
        delivered = False
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
                delivered = True
            except asyncio.QueueFull:
                # A slow viewer does not get a backlog; only live events matter.
                pass
        return delivered
