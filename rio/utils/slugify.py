# -*- coding: utf-8 -*-

from __future__ import absolute_import

__all__ = ['slugify']

from slugify import slugify as _slugify

def slugify(words):
    return _slugify(words).lower()
