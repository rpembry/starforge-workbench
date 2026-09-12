# Reading recent activity

RECENT presents explicit action events with their subject, for example
“Completed: Review fixture”. A metadata edit is “Updated”, even if the action is
already done; it does not imply another completion. Other observations keep their
attributed summary, so an agent report is not promoted to verified completion.
Collector health uses “healthy” rather than claiming a recovery that an initial
heartbeat cannot establish.

Titles use the linked action's current title. Relative time is calculated from
the event's occurrence time at each dashboard refresh. Exact occurrence/recording
times, original summary, source identity, recorder, action/run references, and raw
details remain available under “Details and provenance”. Nothing is deleted,
coalesced, or rewritten in the immutable event store. Missing subjects retain the
original summary. The dashboard API retains original fields and adds presentation
fields; event API records remain unchanged.
