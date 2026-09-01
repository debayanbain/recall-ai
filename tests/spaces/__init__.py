"""Spaces: the container, who may see it, and who may change it.

Kept out of `tests/vault` because a Space is the first thing in this codebase that lets
one user read another's rows. The interesting cases are not "can Bob read Alice's row"
(that is `tests/vault/test_authz.py`) but "Bob is *supposed* to see this, and where does
that stop".
"""
