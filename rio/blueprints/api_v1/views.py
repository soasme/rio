# -*- coding: utf-8 -*-
"""
rio.blueprints.api_v1.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio api v1 view functions.
"""

from flask import jsonify
from flask import request
from sqlalchemy.exc import IntegrityError
from celery.task.http import URL

from rio.core import celery
from rio.models import Webhook
from rio.models import Topic
from rio.tasks import get_webhook as apply_get_webhook
from rio.tasks import post_webhook as apply_post_webhook

from .core import bp


@bp.route('/publish/<topic>', methods=['GET', 'POST'])
def publish(topic):
    """Publish message to topic."""
    topic = Topic.query.filter_by(title=topic).first()
    if not topic:
        return 'not found', 404
    payload = dict(request.values)
    tasks = []
    for webhook in Webhook.query.filter_by(topic_id=topic.id).yield_per(20):
        if webhook.method_id == Webhook.Method.GET:
            res = apply_get_webhook(webhook.url, payload)
            tasks.append({'task_id': res.id, 'url': webhook.url})
        elif webhook.method_id == Webhook.Method.POST:
            res = apply_post_webhook(webhook.url, payload)
            tasks.append({'task_id': res.id, 'url': webhook.url})
    return jsonify(tasks=tasks)

@bp.route('/tasks/<task_id>')
def get_task(task_id):
    from celery.result import AsyncResult
    job = AsyncResult(task_id, app=celery)
    return jsonify(task=dict(
        id=task_id,
        state=job.state,
        result=job.result,
    ))
