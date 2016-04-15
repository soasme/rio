# -*- coding: utf-8 -*-
"""
rio.blueprints.event.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio event view functions.
"""

from uuid import uuid4

from flask import jsonify
from flask import request

from rio.core import cache
from rio.tasks import exec_event
from rio.models  import get_data_by_slug_or_404
from .core import bp


@bp.errorhandler(404)
def not_found(error):
    """Error handler for 404."""
    return jsonify({'message': 'not found'}), 404


@bp.route('/<project_slug>/emit/<action_slug>', methods=['GET', 'POST'])
def emit_event(project_slug, action_slug):
    """Publish message to action.

    Rio will trigger all registered webhooks related to this action and
    trace running process.
    """
    # fetch project
    project = cache.run(get_data_by_slug_or_404,
                        model='project',
                        slug=project_slug,
                        kind='simple')
    project_id = project['id']

    # assert authorization
    if not request.authorization:
        return jsonify({'message': 'unauthorized'}), 401

    username = request.authorization.username
    password = request.authorization.password

    # assert sender
    sender = cache.run(get_data_by_slug_or_404,
                       model='sender',
                       slug=username,
                       kind='sensitive',
                       project_id=project_id,)

    if not sender:
        return jsonify({'message': 'no such sender'}), 401

    # assert sender token
    if sender['token'] != password:
        return jsonify({'message': 'wrong token'}), 401

    action = cache.run(get_data_by_slug_or_404,
                       model='action',
                       slug=action_slug,
                       kind='full',
                       project_id=project_id)

    # assert action belongs to a project
    if action['project']['slug'] != project['slug']:
        return jsonify(message='forbidden'), 403

    # execute event
    event = {'uuid': uuid4(), 'project': project['slug'], 'action': action['slug']}

    if request.headers.get('Content-Type') == 'application/json':
        payload = request.get_json()
    elif request.method == 'POST':
        payload = request.form.to_dict()
    else:
        payload = request.args.to_dict()

    res = exec_event(event, action['webhooks'], payload)

    # response
    return jsonify(message='ok', task={'id': res.id}, event={'uuid': event['uuid']})
