# -*- coding: utf-8 -*-
"""
rio._compact
~~~~~~~~~~~~~
"""

try:
    import cPickle as pickle
except ImportError:
    import pickle  # noqa

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO  # noqa

try:
    import simplejson as json
except ImportError:
    import json
