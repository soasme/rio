# -*- coding: utf-8 -*-
"""
rio.blueprints.event.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio event view functions.
"""

from flask import jsonify
from flask import request

from rio.signals import event_received
from rio.graph import MissingAction
from rio.graph import MissingSender
from rio.graph import WrongSenderSecret
from rio.graph import MissingProject
from rio.graph import NotAllowed
from .core import bp
from .controllers import emit_event as _emit_event

@bp.errorhandler(MissingAction)
@bp.errorhandler(MissingSender)
@bp.errorhandler(MissingProject)
def not_found(error):
    """Error handler for 404."""
    return jsonify({'message': 'not found'}), 404

@bp.errorhandler(MissingSender)
def missing_sender(error):
    return jsonify({'message': 'no such sender'}), 401

@bp.errorhandler(WrongSenderSecret)
def wrong_sender_secret(error):
    return jsonify({'message': 'wrong token'}), 401

@bp.errorhandler(NotAllowed)
def not_allowed(error):
    return jsonify(message='forbidden'), 403


@bp.route('/<project_slug>/emit/<action_slug>', methods=['GET', 'POST'])
def emit_event(project_slug, action_slug):
    """Publish message to action.

    Rio will trigger all registered webhooks related to this action and
    trace running process.
    """

    if request.headers.get('Content-Type') == 'application/json':
        payload = request.get_json()
    elif request.method == 'POST':
        payload = request.form.to_dict()
    else:
        payload = request.args.to_dict()

    if not request.authorization:
        return jsonify({'message': 'unauthorized'}), 401

    sender_name = request.authorization.username
    sender_secret = request.authorization.password

    event_uuid = request.headers.get('X-Rio-Event-UUID') or \
           request.headers.get('X-B3-TraceId')

    data = _emit_event(project_slug, action_slug, payload, sender_name, sender_secret,
                       event_uuid=event_uuid)
    data['message'] = 'ok'

    event_received.send(None, project_slug=project_slug, data=data)

    return jsonify(data)
