"""A deterministic, offline `Scraper` for the walking-skeleton demo (Phase 5).

The live drive (`drive.py`) needs a real CF/NRE and a slow, fragile browser run;
that is exactly what must NOT gate the exam demo. `FixtureScraper` is the fake
that closes the demo/test-fixture open question (log §3): it satisfies the same
`Scraper` seam (D5) the daemon depends on, so **the whole real pipeline —
detector (D8), store (D20), fan-out (D32/D36/D38), rotation (D28) — runs against
it unchanged**, with a scrape reduced to returning canned data.

The canned data is not hand-mocked: `from_recon` parses the **real redacted recon
captures** (`recon/epPrestazioni_redacted.xhtml` + `recon/iframe_slots_redacted.html`)
through the very parsers used in production (`parse_prestazione_confirmation` /
`parse_available_slots`), so the demo exercises the parse half too and its slots
are genuine ground-truth rows (build strategy D).

To demo the *diff* the tool exists to surface, the scraper is **scripted**: it
returns a sequence of slot *frames*, one per successful scrape, so sweep-to-sweep
the detector sees a real "new slot appeared" transition. A frame index that runs
past the end sticks on the last frame (a steady state). A `dead_nres` set makes a
chosen NRE raise `NREInvalidError`, so the D28 representative-rotation path is
demonstrable too.
"""

from collections.abc import Iterable
from pathlib import Path

from salutebot.models import Prestazione, Slot
from salutebot.scraper.base import NREInvalidError, ScrapeResult
from salutebot.scraper.confirmation import parse_prestazione_confirmation
from salutebot.scraper.parser import parse_available_slots

_RECON = Path(__file__).resolve().parent.parent.parent / "recon"
_CONFIRMATION_FIXTURE = _RECON / "epPrestazioni_redacted.xhtml"
_SLOTS_FIXTURE = _RECON / "iframe_slots_redacted.html"


class FixtureScraper:
    """A scripted, offline `Scraper` (D5 seam) for the demo. Attributes private."""

    def __init__(self, prestazione: Prestazione, frames: Iterable[list[Slot]],
                 *, dead_nres: Iterable[str] = ()) -> None:
        # `frames` is the scripted slot sets, returned one per successful scrape;
        # `dead_nres` are NREs that raise `NREInvalidError` (to exercise D28).
        self.__prestazione = prestazione
        self.__frames = [list(frame) for frame in frames]
        if not self.__frames:
            raise ValueError("FixtureScraper needs at least one frame")
        self.__dead = set(dead_nres)
        self.__calls = 0  # advances only on a successful scrape (a dead NRE doesn't consume a frame)

    @classmethod
    def from_recon(cls, *, baseline: int = 4, added: int = 1,
                   dead_nres: Iterable[str] = ()) -> "FixtureScraper":
        """Build from the real redacted recon captures (log §3 demo fixture).

        Parses the captured confirmation + slots pages with the production parsers,
        then scripts three frames from the ground-truth slot list: a `baseline` set,
        the *same* set again (a no-change sweep → no alert), then `baseline + added`
        (the newly-appeared slot → one highlighted alert). Defaults keep the console
        output short and readable; the numbers are clamped to the captured slot count.
        """
        prestazione = parse_prestazione_confirmation(
            _CONFIRMATION_FIXTURE.read_text(encoding="utf-8"))
        if prestazione is None:  # the captured fixture always parses — guard for the type
            raise RuntimeError("recon confirmation fixture did not parse")
        slots = parse_available_slots(_SLOTS_FIXTURE.read_text(encoding="utf-8"))
        base = max(1, min(baseline, len(slots)))
        grown = min(base + max(0, added), len(slots))
        frames = [slots[:base], slots[:base], slots[:grown]]
        return cls(prestazione, frames, dead_nres=dead_nres)

    def scrape(self, cf: str, nre: str) -> ScrapeResult:
        """Return the current frame's slots (D5). Raises `NREInvalidError` for a
        `dead_nres` NRE (D28) — without consuming a frame, so rotation to the next
        subscriber still sees the same sweep's slot set."""
        if nre in self.__dead:
            raise NREInvalidError("fixture: ricetta marcata come non valida (demo D28)")
        frame = self.__frames[min(self.__calls, len(self.__frames) - 1)]
        self.__calls += 1
        return ScrapeResult(prestazione=self.__prestazione, slots=list(frame))
