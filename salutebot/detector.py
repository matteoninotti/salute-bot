"""Detection: per-prestazione slot de-dup (D8, D19/D20).

One cycle, for one prestazione: `new = current_keys - known_keys`, computed in
memory (D8). Newly-seen keys are persisted with `first_seen = now` (permanent,
written once -- row existence *is* the "already alerted" flag); keys still
present get `last_seen` bumped; keys that disappeared are left untouched, and a
later reappearance is read back from `known_slot_keys` as already-known, so it
is never re-alerted (D8).

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
    """Diff one scrape's slots against the store and persist the outcome.

    Per D32, the caller needs the full current availability to build the alert
    (new ones highlighted, not shown in isolation) -- so this returns both the
    complete `current_slots` list and just the newly-seen subset.
    """
    known = store.known_slot_keys(code)
    new_slots: list[Slot] = []
    for slot in current_slots:
        key = slot.slot_key
        if key in known:
            store.touch_slot(code, key, now)
        else:
            store.insert_slot(code, slot, now)
            new_slots.append(slot)
            # Guards a duplicate card within the SAME scrape (defensive: the
            # slot-key collision risk is "negligible" per D16, not "impossible")
            # from being inserted twice and hitting the slots PK.
            known.add(key)

    return DetectionResult(prestazione=code, all_slots=list(current_slots), new_slots=new_slots)
