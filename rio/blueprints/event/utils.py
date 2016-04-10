# -*- coding: utf-8 -*-
"""
rio.blueprints.event.utils
~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from functools import wraps

from flask import request
from flask import jsonify

from rio.models  import validate_sender


def require_sender(f):
    """A decorator that protect emit view function is triggered by a trusted sender.

    Currently, Rio only support Basic Authorization.
    """
    @wraps(f)
    def decorator(*args, **kwargs):
        if not request.authorization:
            return jsonify({'message': 'unauthorized'}), 401

        username = request.authorization.username
        password = request.authorization.password

        if not validate_sender(username, password):
            return jsonify({'message': 'forbidden'}), 403

        return f(*args, **kwargs)
    return decorator
