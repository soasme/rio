# -*- coding: utf-8 -*-
"""
rio.blueprints.event.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio event view functions.
"""

from uuid import uuid4

from flask import jsonify
from flask import request

from rio.tasks import exec_event
from rio.models  import get_data_by_slug_or_404
from .core import bp


@bp.errorhandler(404)
def not_found(error):
    """Error handler for 404."""
    return jsonify({'message': 'not found'}), 404


@bp.route('/<project_slug>/emit/<topic_slug>', methods=['GET', 'POST'])
def emit_topic(project_slug, topic_slug):
    """Publish message to topic.

    Rio will trigger all registered webhooks related to this topic and
    trace running process.
    """
    # fetch project
    project = get_data_by_slug_or_404('project', project_slug, 'simple')
    project_id = project['id']

    # assert authorization
    if not request.authorization:
        return jsonify({'message': 'unauthorized'}), 401

    username = request.authorization.username
    password = request.authorization.password

    # assert sender
    sender = get_data_by_slug_or_404('sender', username, 'sensitive', project_id=project_id)

    if not sender:
        return jsonify({'message': 'no such sender'}), 401

    # assert sender token
    if sender['token'] != password:
        return jsonify({'message': 'wrong token'}), 401

    topic = get_data_by_slug_or_404('topic', topic_slug, 'full', project_id=project_id)

    # assert topic belongs to a project
    if topic['project']['slug'] != project['slug']:
        return jsonify(message='forbidden'), 403

    # execute event
    event = {'uuid': uuid4(), 'project': project['slug'], 'topic': topic['slug']}
    payload = request.values.to_dict()
    res = exec_event(event, topic['webhooks'], payload)

    # response
    return jsonify(message='ok', task={'id': res.id}, event={'uuid': event['uuid']})
