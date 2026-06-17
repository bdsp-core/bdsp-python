"""Runtime bipolar derivations for PSG channels.

The native H5 format produced by ``dev/standardization/scripts/convert_edf_to_h5.py``
stores raw individual electrodes (``c3``, ``m2``, ``e1``, ...) rather than
pre-computed bipolar derivations. Downstream code in this repo (analysis
pipeline, GUI, autoresearch judges, report generation) was written against
common bipolar names like ``c3-m2`` and ``e1-m2``.

Rather than baking derivations into the H5 - which would bloat files, double
write time, and force regeneration whenever the derivation table changes -
we compute them on demand at read time. ``PSGReader`` consults this table
when a caller requests a derived channel, looks up both members in the H5,
and returns ``positive - negative``.

Adding a new derivation here automatically makes it available to every
consumer that reaches data through ``PSGReader.get_channel_slice``.
"""

from __future__ import annotations


# Map: derived_name (lowercase) -> (positive_member, negative_member)
# Both members must exist in the H5 ``signals`` group at the same sampling
# rate. ``PSGReader.channel_names`` will only advertise a derivation when
# both its members are present in a given file.
BIPOLAR_DERIVATIONS: dict[str, tuple[str, str]] = {
    # --- EEG (10-20 montage referenced to contralateral mastoid) ---
    "c3-m2":   ("c3",  "m2"),
    "c4-m1":   ("c4",  "m1"),
    "f3-m2":   ("f3",  "m2"),
    "f4-m1":   ("f4",  "m1"),
    "o1-m2":   ("o1",  "m2"),
    "o2-m1":   ("o2",  "m1"),
    "f7-m2":   ("f7",  "m2"),
    "f8-m1":   ("f8",  "m1"),
    "fp1-m2":  ("fp1", "m2"),
    "fp2-m1":  ("fp2", "m1"),
    "p3-m2":   ("p3",  "m2"),
    "p4-m1":   ("p4",  "m1"),
    "p7-m2":   ("p7",  "m2"),
    "p8-m1":   ("p8",  "m1"),
    "t3-m2":   ("t3",  "m2"),   # legacy 10-20 (T3 == T7 in modern)
    "t4-m1":   ("t4",  "m1"),   # legacy 10-20 (T4 == T8 in modern)
    "t5-m2":   ("t5",  "m2"),
    "t6-m1":   ("t6",  "m1"),
    "t7-m2":   ("t7",  "m2"),   # modern 10-20
    "t8-m1":   ("t8",  "m1"),

    # --- EOG ---
    "e1-m2":   ("e1",  "m2"),
    "e2-m1":   ("e2",  "m1"),

    # --- Leg EMG (active - reference within the same leg) ---
    "lleg":    ("lleg+", "lleg-"),
    "rleg":    ("rleg+", "rleg-"),
}


def available_derivations(raw_channels: set[str] | list[str]) -> list[str]:
    """Return the derived names whose members are all present in ``raw_channels``.

    ``raw_channels`` is the set/list of channel names actually stored in the
    H5 file (lowercase). The result is sorted for stable iteration.
    """
    raw = {c.lower() for c in raw_channels}
    return sorted(
        name for name, (a, b) in BIPOLAR_DERIVATIONS.items()
        if a in raw and b in raw
    )
