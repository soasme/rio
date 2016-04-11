# -*- coding: utf-8 -*-
"""
rio.blueprints.api_v1.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio api v1 view functions.
"""

from uuid import uuid4
from urllib2 import URLError

from flask import jsonify
from flask import request

from celery.result import AsyncResult
from celery.task.http import RemoteExecuteError

from rio.core import celery
from rio.tasks import exec_webhook
from rio.models  import get_data_by_slug_or_404
from rio.signals import event_received
from .core import bp
from .utils import require_sender


@bp.route('/<project_slug>/emit/<topic_slug>', methods=['GET', 'POST'])
@require_sender
def emit_topic(project_slug, topic_slug):
    """Publish message to topic.

    Rio will try to trigger registered webhooks, trace running process.
    """

    #: get context data
    payload = dict(request.values)

    #: fetch project and topic data
    project = get_data_by_slug_or_404('project', project_slug, 'simple')
    topic = get_data_by_slug_or_404('topic', topic_slug, 'full')

    #: TODO: assert project belongs to sender

    #: assert topic belongs to a project
    if topic['project']['slug'] != project['slug']:
        return jsonify(message='forbidden'), 403

    #: run topic webhooks
    webhooks = topic['webhooks']
    tasks = [exec_webhook(w, payload) for w in webhooks]

    #: trigger event
    event_id = uuid4()
    event_received.send('bp-event-emittopic', event_id=event_id, tasks=tasks, payload=payload)

    #: response
    resp = jsonify(tasks=tasks)
    resp.headers['X-RIO-EVENT-ID'] = event_id
    return resp



@bp.route('/<project_slug>/status/<event_id>')
@require_sender
def get_event(project_slug, event_id):
    """Get task information.
    find from database first: is it an recorded event?
      fetch from database
    find from queue second: is it running?
      inspect status
    abort 404: non exist or expired.
    """
    return jsonify(
        status='RAN',
        payload={},
        tasks=[
            {
                'status': 'FAILURE',
                'method': 'GET',
                'url': 'http://example.org'
            }
        ]
    )
