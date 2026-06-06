"""
ka9q-radio compatibility pin.

Exposes the ka9q-radio commit hash that this version of ka9q-python
was validated against.  Intended for consumption by ka9q-update and
other deployment tooling.

Auto-updated by: scripts/sync_types.py --apply

Usage:
    from ka9q.compat import KA9Q_RADIO_COMMIT
    print(f"Compatible with ka9q-radio at {KA9Q_RADIO_COMMIT}")
"""

KA9Q_RADIO_COMMIT: str = "6e0e132130e70fecdbb1931888de34e180f9bfa9"
