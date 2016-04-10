# -*- coding: utf-8 -*-
"""
rio.blueprints.event.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio event view functions.
"""
import os
from urllib2 import URLError

from flask import jsonify
from flask import request
from flask import current_app

from celery.result import AsyncResult
from celery.task.http import RemoteExecuteError

from rio.core import celery
from rio.tasks import exec_webhook
from rio.models  import get_data_by_slug_or_404
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

    #: response
    return jsonify(tasks=tasks)



@bp.route('/<project_slug>/tasks/<task_id>')
@require_sender
def get_task(project_slug, task_id):
    """Get task information.
    """
    job = AsyncResult(task_id, app=celery)
    if job.successful():
        result = job.get()
    elif job.failed():
        try:
            job.get()
        except RemoteExecuteError as exception:
            result = exception.message
        except URLError as exception:
            result = 'URLError: ' + str(exception)
    else:
        result = None
    return jsonify(task=dict(
        id=task_id,
        status=job.status,
        retval=result,
    ))
