/* Wholly invented public teaching corpus. See the provenance notice in the demo. */
window.BRAIN_DEMO = {
  "version": 1,
  "title": "Morrow Observatory · Release 1.4",
  "provenance": "Wholly invented teaching corpus. All roles, project records, paths, observations and check results are fictional. No private Brain data is loaded. Context selection is an illustrative browser model, not an engine benchmark.",
  "nodes": [
    {
      "id": "observatory",
      "title": "Morrow Observatory",
      "family": "concept",
      "kind": "knowledge",
      "owner": "Release Steward",
      "summary": "A fictional ridge-top observatory turns weather packets into a public station timeline.",
      "excerpt": "This example project ships a dashboard for a small invented sensor network. Its release records connect implementation, evidence, decisions and operating guidance.",
      "source": "example/project/overview.md",
      "status": "current",
      "x": 510,
      "y": 65
    },
    {
      "id": "release",
      "title": "Release 1.4",
      "family": "document",
      "kind": "knowledge",
      "owner": "Release Steward",
      "summary": "The release adds recoverable packet delivery and keeps older stations readable.",
      "excerpt": "The candidate is ready only when replay, schema compatibility, time ordering and retention checks pass. Documentation and a rehearsed rollback accompany the software.",
      "source": "example/releases/1.4/plan.md",
      "status": "current",
      "x": 505,
      "y": 215
    },
    {
      "id": "intake",
      "title": "Packet intake",
      "family": "code",
      "kind": "knowledge",
      "owner": "Signal Steward",
      "summary": "Validates station packets before handing them to the replay buffer.",
      "excerpt": "Reject malformed envelopes with a visible reason. Acknowledgement means the packet reached durable local storage, not that the dashboard has already displayed it.",
      "source": "example/code/packet-intake.js",
      "status": "current",
      "x": 280,
      "y": 205
    },
    {
      "id": "buffer",
      "title": "Replay buffer",
      "family": "code",
      "kind": "knowledge",
      "owner": "Replay Steward",
      "summary": "Stores accepted packets until the publishing worker confirms delivery.",
      "excerpt": "The invented implementation retains a bounded queue during a connection gap. Retries keep the original packet identity so replay can avoid duplicate timeline entries.",
      "source": "example/code/replay-buffer.js",
      "status": "current",
      "x": 365,
      "y": 305
    },
    {
      "id": "schema",
      "title": "Packet schema v2",
      "family": "document",
      "kind": "knowledge",
      "owner": "Signal Steward",
      "summary": "Defines station ID, packet ID, observation time and measurement fields.",
      "excerpt": "An observation timestamp belongs to the station reading; a received timestamp belongs to the intake. Version 1 packets remain readable through the compatibility adapter.",
      "source": "example/reference/packet-schema-v2.md",
      "status": "current",
      "x": 215,
      "y": 375
    },
    {
      "id": "dashboard",
      "title": "Station timeline",
      "family": "code",
      "kind": "knowledge",
      "owner": "Signal Steward",
      "summary": "Displays observations in event-time order with explicit gaps.",
      "excerpt": "A delayed packet can fill an earlier gap. An unavailable reading stays unknown; the interface must not quietly substitute a zero measurement.",
      "source": "example/code/station-timeline.js",
      "status": "current",
      "x": 90,
      "y": 205
    },
    {
      "id": "gate",
      "title": "Release checks",
      "family": "automation",
      "kind": "knowledge",
      "owner": "Release Steward",
      "summary": "Collects the required checks for the 1.4 candidate.",
      "excerpt": "A failed required check holds this fictional candidate. Passing the checks supplies evidence for review; it does not assert that a deployment has happened.",
      "source": "example/automation/release-checks.yml",
      "status": "current",
      "x": 665,
      "y": 295
    },
    {
      "id": "rollback",
      "title": "Rollback guide",
      "family": "document",
      "kind": "knowledge",
      "owner": "Replay Steward",
      "summary": "Describes returning to the previous worker without discarding queued packets.",
      "excerpt": "Pause publishing, preserve the queue, restore the previous worker and resume with the same packet IDs. The rehearsal records whether this sequence actually preserved observations.",
      "source": "example/runbooks/rollback.md",
      "status": "current",
      "x": 420,
      "y": 595
    },
    {
      "id": "guide",
      "title": "Station upgrade guide",
      "family": "document",
      "kind": "knowledge",
      "owner": "Guide Steward",
      "summary": "Explains the visible changes and the safe station upgrade sequence.",
      "excerpt": "Upgrade one station first, check its timeline, then continue. Readers learn why missing readings appear as gaps and why recovered readings may arrive later.",
      "source": "example/guides/station-upgrade.md",
      "status": "current",
      "x": 340,
      "y": 495
    },
    {
      "id": "direct-decision",
      "title": "Direct delivery only",
      "family": "document",
      "kind": "memory",
      "owner": "Replay Steward",
      "summary": "An earlier decision sent readings directly to the publisher without local replay.",
      "excerpt": "This fictional decision favored a simpler initial worker. It is retained as historical context and is superseded by the buffered-delivery decision after the outage drill.",
      "source": "example/memory/decisions/direct-delivery.md",
      "status": "superseded",
      "x": 845,
      "y": 65
    },
    {
      "id": "buffer-decision",
      "title": "Adopt buffered delivery",
      "family": "document",
      "kind": "memory",
      "owner": "Replay Steward",
      "summary": "Replaces direct-only delivery with a bounded replay queue.",
      "excerpt": "The outage drill exposed missing observations after a connection gap. Accept bounded local storage and identity-preserving retries; retain evidence of the previous choice and its replacement.",
      "source": "example/memory/decisions/buffered-delivery.md",
      "status": "current",
      "x": 800,
      "y": 210
    },
    {
      "id": "outage-drill",
      "title": "Connection-gap drill",
      "family": "document",
      "kind": "memory",
      "owner": "Replay Steward",
      "summary": "An invented drill disconnects the publisher while stations continue sending.",
      "excerpt": "The direct-only worker leaves a visible gap after reconnecting. This is a scripted example observation, not a measured result from a real observatory or the Brain engine.",
      "source": "example/memory/experiences/connection-gap-drill.md",
      "status": "current",
      "x": 940,
      "y": 155
    },
    {
      "id": "replay-test",
      "title": "Replay check",
      "family": "document",
      "kind": "memory",
      "owner": "Replay Steward",
      "summary": "The invented replay check verifies one timeline entry per accepted packet.",
      "excerpt": "The fixture includes repeated delivery attempts after reconnection. In this fictional result, every accepted packet appears once and retains its original observation time.",
      "source": "example/evidence/replay-check.md",
      "status": "current",
      "x": 665,
      "y": 410
    },
    {
      "id": "schema-test",
      "title": "Compatibility check",
      "family": "document",
      "kind": "memory",
      "owner": "Signal Steward",
      "summary": "An invented check exercises both packet versions through the adapter.",
      "excerpt": "The example fixture yields the same supported fields for v1 and v2 packets. This supports keeping the adapter for this release; unsupported fields remain explicitly absent.",
      "source": "example/evidence/compatibility-check.md",
      "status": "current",
      "x": 800,
      "y": 395
    },
    {
      "id": "clock-test",
      "title": "Time-order check",
      "family": "document",
      "kind": "memory",
      "owner": "Signal Steward",
      "summary": "An invented check verifies ordering when packets arrive late.",
      "excerpt": "The example sends one older observation after a newer observation. The timeline places it by observation time and preserves received time as separate diagnostic context.",
      "source": "example/evidence/time-order-check.md",
      "status": "current",
      "x": 940,
      "y": 425
    },
    {
      "id": "retention-test",
      "title": "Queue-limit check",
      "family": "document",
      "kind": "memory",
      "owner": "Replay Steward",
      "summary": "An invented check exercises the buffer at its configured retention limit.",
      "excerpt": "When the example queue reaches its limit, intake reports backpressure rather than silently deleting accepted packets. The limit and overflow behavior belong in the operating notes.",
      "source": "example/evidence/queue-limit-check.md",
      "status": "current",
      "x": 820,
      "y": 505
    },
    {
      "id": "review",
      "title": "Candidate review",
      "family": "document",
      "kind": "memory",
      "owner": "Release Steward",
      "summary": "Records the fictional review of the release decisions and required checks.",
      "excerpt": "The reviewer accepts buffered delivery and temporary v1 compatibility for this candidate, subject to the recorded release checks. This record is not a claim of real approval or publication.",
      "source": "example/memory/reviews/candidate-1.4.md",
      "status": "current",
      "x": 695,
      "y": 575
    },
    {
      "id": "rehearsal",
      "title": "Rollback rehearsal",
      "family": "document",
      "kind": "memory",
      "owner": "Release Steward",
      "summary": "Records an invented rehearsal with queued observations present.",
      "excerpt": "The rehearsal follows the rollback guide and checks the timeline after resuming. The example result preserves packet identity and queued observations across the worker change.",
      "source": "example/evidence/rollback-rehearsal.md",
      "status": "current",
      "x": 555,
      "y": 505
    },
    {
      "id": "signal-agent",
      "title": "Signal Steward",
      "family": "agent",
      "kind": "agent",
      "owner": "Signal Steward",
      "summary": "Fictional role responsible for packet meaning and the station timeline.",
      "excerpt": "Maintains intake, the packet schema and the compatibility adapter. This is an invented role label, not a real person, agent identity or operating assignment.",
      "source": "example/roles/signal-steward.md",
      "status": "current",
      "x": 180,
      "y": 80
    },
    {
      "id": "replay-agent",
      "title": "Replay Steward",
      "family": "agent",
      "kind": "agent",
      "owner": "Replay Steward",
      "summary": "Fictional role responsible for delivery recovery and queue behavior.",
      "excerpt": "Maintains the replay buffer, monitoring notes and rollback guide. The role links implementation ownership to the example decisions and evidence.",
      "source": "example/roles/replay-steward.md",
      "status": "current",
      "x": 390,
      "y": 110
    },
    {
      "id": "release-agent",
      "title": "Release Steward",
      "family": "agent",
      "kind": "agent",
      "owner": "Release Steward",
      "summary": "Fictional role responsible for candidate review and release sequencing.",
      "excerpt": "Maintains the release plan and checks. A role connection tells a reader who would steward a record within the invented scenario.",
      "source": "example/roles/release-steward.md",
      "status": "current",
      "x": 615,
      "y": 175
    },
    {
      "id": "guide-agent",
      "title": "Guide Steward",
      "family": "agent",
      "kind": "agent",
      "owner": "Guide Steward",
      "summary": "Fictional role responsible for station guidance and release notes.",
      "excerpt": "Maintains reader-facing explanations of recovery and compatibility. The role is included to make ownership traversable in the example graph.",
      "source": "example/roles/guide-steward.md",
      "status": "current",
      "x": 180,
      "y": 595
    },
    {
      "id": "missing-data",
      "title": "Missing means unknown",
      "family": "concept",
      "kind": "knowledge",
      "owner": "Signal Steward",
      "summary": "A reference principle: an unavailable reading is not a measured zero.",
      "excerpt": "Keep gaps visible until a valid observation is available. This invented project uses the principle when designing its timeline, upgrade guide and earlier delivery choice.",
      "source": "example/reference/missing-observations.md",
      "status": "current",
      "x": 65,
      "y": 90
    },
    {
      "id": "idempotency",
      "title": "Stable packet identity",
      "family": "concept",
      "kind": "knowledge",
      "owner": "Replay Steward",
      "summary": "A reference rule for recognizing repeated delivery of the same observation.",
      "excerpt": "Retries preserve the original station ID and packet ID. Delivery attempts may repeat while the resulting timeline observation remains a single entry.",
      "source": "example/reference/packet-identity.md",
      "status": "current",
      "x": 430,
      "y": 400
    },
    {
      "id": "event-time",
      "title": "Observation time",
      "family": "concept",
      "kind": "knowledge",
      "owner": "Signal Steward",
      "summary": "A reference distinction between when a reading happened and when it arrived.",
      "excerpt": "Order the timeline by observation time. Retain receipt time for diagnosis so a late delivery does not rewrite when the weather was observed.",
      "source": "example/reference/observation-time.md",
      "status": "current",
      "x": 70,
      "y": 455
    },
    {
      "id": "outage-lesson",
      "title": "Recovery needs a queue",
      "family": "document",
      "kind": "memory",
      "owner": "Replay Steward",
      "summary": "Captures the lesson drawn from the fictional connection-gap drill.",
      "excerpt": "A reconnect alone cannot recover a reading that was never retained. Preserve accepted packets locally and make the retention boundary visible to operators.",
      "source": "example/memory/lessons/recovery-needs-a-queue.md",
      "status": "current",
      "x": 940,
      "y": 295
    },
    {
      "id": "monitor",
      "title": "Queue monitoring notes",
      "family": "document",
      "kind": "knowledge",
      "owner": "Replay Steward",
      "summary": "Defines the queue signals to watch during the candidate rollout.",
      "excerpt": "Track queue depth, oldest unconfirmed packet and rejection reasons. A growing queue needs attention even when the dashboard continues showing its last received observation.",
      "source": "example/runbooks/queue-monitoring.md",
      "status": "current",
      "x": 570,
      "y": 610
    },
    {
      "id": "fixture",
      "title": "Two-station fixture",
      "family": "code",
      "kind": "knowledge",
      "owner": "Signal Steward",
      "summary": "Invented packets cover two stations, two schema versions and a reconnect.",
      "excerpt": "The fixture contains only fabricated station IDs and measurements. It includes a repeated packet and a late observation so compatibility and replay checks share explicit inputs.",
      "source": "example/fixtures/two-station-packets.json",
      "status": "current",
      "x": 230,
      "y": 485
    },
    {
      "id": "adapter",
      "title": "Version 1 adapter",
      "family": "code",
      "kind": "knowledge",
      "owner": "Signal Steward",
      "summary": "Maps the older packet envelope into the current supported shape.",
      "excerpt": "Preserve packet identity and observation time when mapping a v1 envelope. Do not invent measurements for fields the older version never supplied.",
      "source": "example/code/version-1-adapter.js",
      "status": "current",
      "x": 90,
      "y": 320
    },
    {
      "id": "compat-decision",
      "title": "Keep v1 for this release",
      "family": "document",
      "kind": "memory",
      "owner": "Signal Steward",
      "summary": "Keeps the compatibility adapter for the 1.4 station upgrade window.",
      "excerpt": "The invented compatibility check supports retaining readable older packets while stations upgrade in stages. Review removal separately in a later release.",
      "source": "example/memory/decisions/keep-version-1.md",
      "status": "current",
      "x": 640,
      "y": 75
    },
    {
      "id": "notes",
      "title": "Release notes",
      "family": "document",
      "kind": "knowledge",
      "owner": "Guide Steward",
      "summary": "Summarizes buffered recovery, explicit gaps and the compatibility window.",
      "excerpt": "Readers see what changed and where to inspect the supporting decisions. The notes describe this fictional candidate and do not announce a real software deployment.",
      "source": "example/releases/1.4/notes.md",
      "status": "current",
      "x": 320,
      "y": 405
    },
    {
      "id": "publish",
      "title": "Publish sequence",
      "family": "automation",
      "kind": "knowledge",
      "owner": "Release Steward",
      "summary": "Orders the candidate checks, monitoring preparation and rollback readiness.",
      "excerpt": "Run required checks, review evidence, confirm the monitoring plan and rehearse rollback before proceeding. This graph is a static teaching example; it runs none of those actions.",
      "source": "example/automation/publish-sequence.yml",
      "status": "current",
      "x": 530,
      "y": 370
    }
  ],
  "edges": [
    {
      "from": "release",
      "to": "observatory",
      "predicate": "part_of",
      "explanation": "Release 1.4 belongs to the fictional observatory project."
    },
    {
      "from": "intake",
      "to": "observatory",
      "predicate": "part_of",
      "explanation": "Packet intake is a component of the observatory software."
    },
    {
      "from": "dashboard",
      "to": "observatory",
      "predicate": "part_of",
      "explanation": "The station timeline is a component of the observatory software."
    },
    {
      "from": "release",
      "to": "gate",
      "predicate": "depends_on",
      "explanation": "The release candidate requires the recorded checks."
    },
    {
      "from": "release",
      "to": "guide",
      "predicate": "depends_on",
      "explanation": "Station operators need upgrade guidance for this candidate."
    },
    {
      "from": "release",
      "to": "notes",
      "predicate": "depends_on",
      "explanation": "The candidate includes an explanation of its changes."
    },
    {
      "from": "release",
      "to": "publish",
      "predicate": "depends_on",
      "explanation": "The release plan relies on an explicit publishing sequence."
    },
    {
      "from": "release",
      "to": "buffer-decision",
      "predicate": "cites",
      "explanation": "The release plan references its recovery decision."
    },
    {
      "from": "release",
      "to": "compat-decision",
      "predicate": "cites",
      "explanation": "The release plan references the compatibility window."
    },
    {
      "from": "release",
      "to": "release-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional release role stewards this plan."
    },
    {
      "from": "intake",
      "to": "schema",
      "predicate": "depends_on",
      "explanation": "Validation requires a defined packet envelope."
    },
    {
      "from": "intake",
      "to": "buffer",
      "predicate": "depends_on",
      "explanation": "Accepted packets must reach the replay queue before acknowledgement."
    },
    {
      "from": "intake",
      "to": "signal-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional signal role stewards packet intake."
    },
    {
      "from": "buffer",
      "to": "schema",
      "predicate": "depends_on",
      "explanation": "Queued records retain the packet identity and observation fields."
    },
    {
      "from": "buffer",
      "to": "buffer-decision",
      "predicate": "cites",
      "explanation": "The buffer implementation references the decision that introduced it."
    },
    {
      "from": "buffer",
      "to": "idempotency",
      "predicate": "cites",
      "explanation": "The buffer references the stable-identity retry rule."
    },
    {
      "from": "buffer",
      "to": "replay-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional replay role stewards delivery recovery."
    },
    {
      "from": "schema",
      "to": "event-time",
      "predicate": "cites",
      "explanation": "The schema distinguishes observation time from receipt time."
    },
    {
      "from": "schema",
      "to": "signal-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional signal role stewards packet meaning."
    },
    {
      "from": "dashboard",
      "to": "adapter",
      "predicate": "depends_on",
      "explanation": "The timeline can read older packets through the adapter."
    },
    {
      "from": "dashboard",
      "to": "missing-data",
      "predicate": "cites",
      "explanation": "The timeline references the principle behind visible gaps."
    },
    {
      "from": "dashboard",
      "to": "signal-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional signal role stewards the timeline."
    },
    {
      "from": "gate",
      "to": "replay-test",
      "predicate": "depends_on",
      "explanation": "The release checks require the replay result."
    },
    {
      "from": "gate",
      "to": "schema-test",
      "predicate": "depends_on",
      "explanation": "The release checks require the compatibility result."
    },
    {
      "from": "gate",
      "to": "clock-test",
      "predicate": "depends_on",
      "explanation": "The release checks require the time-order result."
    },
    {
      "from": "gate",
      "to": "retention-test",
      "predicate": "depends_on",
      "explanation": "The release checks require the queue-limit result."
    },
    {
      "from": "gate",
      "to": "review",
      "predicate": "depends_on",
      "explanation": "The release checks require candidate review."
    },
    {
      "from": "gate",
      "to": "rehearsal",
      "predicate": "depends_on",
      "explanation": "The release checks require rollback evidence."
    },
    {
      "from": "gate",
      "to": "release-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional release role stewards the check sequence."
    },
    {
      "from": "rollback",
      "to": "buffer",
      "predicate": "depends_on",
      "explanation": "Rollback must preserve the replay queue."
    },
    {
      "from": "rollback",
      "to": "replay-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional replay role stewards recovery instructions."
    },
    {
      "from": "guide",
      "to": "missing-data",
      "predicate": "cites",
      "explanation": "The guide explains why unavailable observations remain gaps."
    },
    {
      "from": "guide",
      "to": "rollback",
      "predicate": "cites",
      "explanation": "The guide points operators to the recovery procedure."
    },
    {
      "from": "guide",
      "to": "guide-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional guide role stewards station instructions."
    },
    {
      "from": "buffer-decision",
      "to": "direct-decision",
      "predicate": "supersedes",
      "explanation": "Buffered delivery replaces the historical direct-only choice."
    },
    {
      "from": "buffer-decision",
      "to": "outage-drill",
      "predicate": "cites",
      "explanation": "The replacement decision references the drill that exposed the gap."
    },
    {
      "from": "buffer-decision",
      "to": "outage-lesson",
      "predicate": "cites",
      "explanation": "The replacement decision records the lesson behind retaining packets."
    },
    {
      "from": "outage-drill",
      "to": "buffer-decision",
      "predicate": "supports",
      "explanation": "The invented gap observation supports adding retained replay."
    },
    {
      "from": "replay-test",
      "to": "buffer-decision",
      "predicate": "supports",
      "explanation": "The invented retry result supports the buffered-delivery choice."
    },
    {
      "from": "replay-test",
      "to": "fixture",
      "predicate": "depends_on",
      "explanation": "The replay check uses the invented repeated-packet fixture."
    },
    {
      "from": "schema-test",
      "to": "schema",
      "predicate": "depends_on",
      "explanation": "The compatibility check requires the supported packet shape."
    },
    {
      "from": "schema-test",
      "to": "fixture",
      "predicate": "depends_on",
      "explanation": "The compatibility check uses both invented packet versions."
    },
    {
      "from": "clock-test",
      "to": "event-time",
      "predicate": "cites",
      "explanation": "The time-order check references the observation-time rule."
    },
    {
      "from": "retention-test",
      "to": "buffer",
      "predicate": "depends_on",
      "explanation": "The queue-limit check exercises the replay buffer."
    },
    {
      "from": "review",
      "to": "compat-decision",
      "predicate": "cites",
      "explanation": "The candidate review examines the compatibility choice."
    },
    {
      "from": "review",
      "to": "buffer-decision",
      "predicate": "cites",
      "explanation": "The candidate review examines the recovery choice."
    },
    {
      "from": "rehearsal",
      "to": "rollback",
      "predicate": "cites",
      "explanation": "The rehearsal records the procedure it followed."
    },
    {
      "from": "rehearsal",
      "to": "release",
      "predicate": "supports",
      "explanation": "The invented recovery rehearsal supports candidate readiness."
    },
    {
      "from": "direct-decision",
      "to": "missing-data",
      "predicate": "cites",
      "explanation": "The earlier decision acknowledged that missing readings would remain gaps."
    },
    {
      "from": "outage-lesson",
      "to": "outage-drill",
      "predicate": "cites",
      "explanation": "The lesson references the invented experience it came from."
    },
    {
      "from": "monitor",
      "to": "buffer",
      "predicate": "depends_on",
      "explanation": "The monitoring notes describe replay-queue signals."
    },
    {
      "from": "monitor",
      "to": "replay-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional replay role stewards monitoring guidance."
    },
    {
      "from": "fixture",
      "to": "schema",
      "predicate": "cites",
      "explanation": "The fabricated packets follow the example packet shape."
    },
    {
      "from": "adapter",
      "to": "schema",
      "predicate": "depends_on",
      "explanation": "The adapter maps older envelopes into the supported schema."
    },
    {
      "from": "adapter",
      "to": "compat-decision",
      "predicate": "cites",
      "explanation": "The adapter references the decision to keep v1 readable."
    },
    {
      "from": "adapter",
      "to": "signal-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional signal role stewards the compatibility adapter."
    },
    {
      "from": "compat-decision",
      "to": "schema-test",
      "predicate": "cites",
      "explanation": "The compatibility decision references its invented check."
    },
    {
      "from": "schema-test",
      "to": "compat-decision",
      "predicate": "supports",
      "explanation": "The invented dual-version result supports keeping the adapter."
    },
    {
      "from": "notes",
      "to": "buffer-decision",
      "predicate": "cites",
      "explanation": "The notes explain the delivery recovery change."
    },
    {
      "from": "notes",
      "to": "compat-decision",
      "predicate": "cites",
      "explanation": "The notes explain the temporary compatibility window."
    },
    {
      "from": "notes",
      "to": "guide-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional guide role stewards release explanations."
    },
    {
      "from": "publish",
      "to": "gate",
      "predicate": "depends_on",
      "explanation": "The publishing sequence requires candidate checks."
    },
    {
      "from": "publish",
      "to": "monitor",
      "predicate": "depends_on",
      "explanation": "The publishing sequence requires monitoring preparation."
    },
    {
      "from": "publish",
      "to": "rollback",
      "predicate": "depends_on",
      "explanation": "The publishing sequence requires a recovery procedure."
    },
    {
      "from": "publish",
      "to": "release-agent",
      "predicate": "maintained_by",
      "explanation": "The fictional release role stewards publishing order."
    }
  ],
  "predicates": {
    "depends_on": {
      "label": "depends on",
      "description": "The source relies on the target to work or proceed."
    },
    "maintained_by": {
      "label": "maintained by",
      "description": "The target agent role stewards the source record or component."
    },
    "cites": {
      "label": "cites",
      "description": "The source explicitly references the target record or principle."
    },
    "supports": {
      "label": "supports",
      "description": "The source is asserted to provide evidence for the target; inspect its text."
    },
    "supersedes": {
      "label": "supersedes",
      "description": "The source replaces the target while preserving the earlier record as history."
    },
    "part_of": {
      "label": "part of",
      "description": "The source is a component or release of the target project."
    }
  },
  "scenarios": [
    {
      "id": "release-readiness",
      "title": "What does the release need?",
      "question": "What must be checked before release 1.4 can proceed?",
      "seedIds": [
        "release",
        "gate"
      ],
      "why": "Start at the candidate and its checks, then follow dependencies to evidence, operating guidance and ownership. A smaller budget shows why a context pack cannot include the whole project."
    },
    {
      "id": "decision-change",
      "title": "Why did the decision change?",
      "question": "Why was direct-only delivery replaced by buffered delivery?",
      "seedIds": [
        "direct-decision",
        "buffer-decision"
      ],
      "why": "Follow the supersedes relationship to the current choice, then inspect the connection-gap drill and replay evidence. The earlier decision remains visible in the graph but is excluded from a current context pack."
    },
    {
      "id": "late-observations",
      "title": "How do late readings stay correct?",
      "question": "How do older and delayed station packets remain readable without duplicate observations?",
      "seedIds": [
        "dashboard",
        "buffer"
      ],
      "why": "Connect the timeline and queue to packet identity, observation time, schema compatibility and their invented checks. Knowledge references explain the rules; memory records show decisions and example experience."
    }
  ]
};

