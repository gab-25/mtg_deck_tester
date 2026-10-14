"""Ownership: every deck and match belongs to the user who created it."""

from django.shortcuts import get_object_or_404


def get_owned_or_404(model, user, pk):
    """Returns ``user``'s ``model`` row with primary key ``pk``, or raises 404.

    Someone else's row is a 404 rather than a 403, so its existence stays
    undisclosed.
    """
    return get_object_or_404(model, pk=pk, owner=user)
