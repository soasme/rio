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
from rio.tasks import exec_event
from rio.models  import get_data_by_slug_or_404
from rio.models  import get_data_by_hex_uuid_or_404
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

    #: TODO: assert sender has authority to emit topic in project

    #: assert topic belongs to a project
    if topic['project']['slug'] != project['slug']:
        return jsonify(message='forbidden'), 403

    #: execute event
    event = {'uuid': uuid4(), 'project': project['slug'], 'topic': topic['slug']}
    res = exec_event(event, topic['webhooks'], payload)

    #: response
    return jsonify(message='ok', task={'id': res.id}, event={'uuid': event['uuid']})
