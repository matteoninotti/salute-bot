"""Detection: per-prestazione slot de-dup (D8, D19/D20, D32, D36).

One cycle, for one prestazione: `new = current_keys - known_keys`, computed in
memory (D8). This module **detects only** -- it does not persist newly-seen
slots. Per D36 (at-least-once alerting) a new slot's row is written *after* a
successful alert send, not before, so persistence of the new keys is the alert
fan-out's job (`store.record_new_slots`, called post-send). What detection does
touch is `last_seen` on keys that are still present: that bump is auxiliary and
independent of alerting (D36), so it happens every cycle regardless of send
outcome. Keys that disappeared are left untouched, and a later reappearance is
read back from `known_slot_keys` as already-known once it has been persisted, so
it is never re-alerted (D8).

`slots` is the de-dup memory **per prestazione**, not per user (D20): a slot
found via any subscriber's scrape is detected, alerted, and stored exactly once
regardless of how many users watch that code. Resolving "who to notify" from
the result is a separate concern (the alert fan-out step joins
`new_slots -> targets -> users`), deliberately not this module's job.
"""

from salutebot.models import DetectionResult, Slot
from salutebot.store import Store


def detect_new_slots(
    store: Store, code: str, current_slots: list[Slot], now: float | None = None
) -> DetectionResult:
    """Diff one scrape's slots against the store; bump `last_seen`, return the diff.

    Newly-seen slots are **not** persisted here -- the fan-out records them only
    after a successful send (D36). Per D32 the caller needs the full current
    availability to build the alert (new ones highlighted, not shown in
    isolation), so this returns both the complete current set and the new subset.

    `all_slots` is de-duped by `slot_key`: a scrape can (negligibly, D16) expose
    two cards with the same natural key, and D32's "current set" is a *set* of
    keys -- so the returned list carries each key once, never a doubled row.
    """
    known = store.known_slot_keys(code)
    seen: set[str] = set()
    current: list[Slot] = []
    new_slots: list[Slot] = []
    for slot in current_slots:
        key = slot.slot_key
        if key in seen:
            continue  # duplicate card within this same scrape -- collapse to one
        seen.add(key)
        current.append(slot)
        if key in known:
            store.touch_slot(code, key, now)  # last_seen bump, independent of alerting (D36)
        else:
            new_slots.append(slot)  # NOT persisted here -- fan-out records post-send (D36)

    return DetectionResult(prestazione=code, all_slots=current, new_slots=new_slots)
