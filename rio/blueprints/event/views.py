# -*- coding: utf-8 -*-
"""
rio.blueprints.api_v1.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio api v1 view functions.
"""

from uuid import uuid4

from flask import jsonify
from flask import request

from rio.tasks import exec_event
from rio.models  import get_data_by_slug_or_404
from rio.models  import validate_sender
from .core import bp


@bp.route('/<project_slug>/emit/<topic_slug>', methods=['GET', 'POST'])
def emit_topic(project_slug, topic_slug):
    """Publish message to topic.

    Rio will try to trigger registered webhooks, trace running process.
    """
    # load payload
    payload = dict(request.values)

    # fetch project
    project = get_data_by_slug_or_404('project', project_slug, 'simple')

    # assert authorization
    if not request.authorization:
        return jsonify({'message': 'unauthorized'}), 401

    username = request.authorization.username
    password = request.authorization.password

    # assert permission
    if not validate_sender(project['id'], username, password):
        return jsonify({'message': 'forbidden'}), 403

    topic = get_data_by_slug_or_404('topic', topic_slug, 'full')

    # assert topic belongs to a project
    if topic['project']['slug'] != project['slug']:
        return jsonify(message='forbidden'), 403

    # execute event
    event = {'uuid': uuid4(), 'project': project['slug'], 'topic': topic['slug']}
    res = exec_event(event, topic['webhooks'], payload)

    # response
    return jsonify(message='ok', task={'id': res.id}, event={'uuid': event['uuid']})
