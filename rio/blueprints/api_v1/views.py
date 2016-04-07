# -*- coding: utf-8 -*-
"""
rio.blueprints.api_v1.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio api v1 view functions.
"""
import os

from flask import jsonify
from flask import request
from flask import current_app

from celery.result import AsyncResult
from celery.task.http import RemoteExecuteError

from rio.core import celery
from rio.tasks import get_webhook as apply_get_webhook
from rio.tasks import post_webhook as apply_post_webhook

from .core import bp

AVAILABLE_METHODS = {'GET', 'POST'}

def _load_yaml(rcfile, topic):
    """Load yaml config

    :param rcfile: yaml file path
    :param topic: as yaml file is actually a dict, topic is the key of this dict.
    :return: return a list of tuple contained (method, url)
    """
    import yaml
    try:
        with open(rcfile) as f:
            webhooks = yaml.load(f.read())
            webhooks = webhooks.get(topic) or []
            return [
                (w.get('method', 'GET'), w.get('url'))
                for w in webhooks
            ]
    except IOError:
        pass

def _load_webhooks(topic):
    """Load webhooks by topic.

    * Load from application config `RIO_WEBHOOKS`
    * Load from yaml file `.rio.yml` in cwd/home.
    * If `RIO_HOTRELOAD_WEBHOOKS` is set to True, Rio will try to load file per request.
    """
    if 'RIO_WEBHOOKS' in current_app.config:
        return (current_app.config.get('RIO_WEBHOOKS') or {}).get(topic)
    elif os.path.exists(os.path.join(os.getcwd(), '.rio.yml')):
        return _load_yaml((os.path.join(os.getcwd(), '.rio.yml')), topic)
    elif os.path.exists(os.path.join(os.environ.get('HOME'), '.rio.yml')):
        return _load_yaml(os.path.join(os.environ.get('HOME'), '.rio.yml'), topic)
    else:
        return []


@bp.route('/publish/<topic>', methods=['GET', 'POST'])
def publish(topic):
    """Publish message to topic."""
    #: detect whether topic is in app config
    webhooks = _load_webhooks(topic)
    if not webhooks:
        return jsonify(tasks=[])

    tasks = []
    payload = dict(request.values)
    for method, url in webhooks:
        method = method.upper()
        if method not in AVAILABLE_METHODS:
            continue
        if method == 'GET':
            runner = apply_get_webhook
        elif method == 'POST':
            runner = apply_post_webhook
        res = runner(url, payload)
        tasks.append({'task_id': res.id, 'url': url})
    return jsonify(tasks=tasks)

@bp.route('/tasks/<task_id>')
def get_task(task_id):
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
    else:
        result = None
    return jsonify(task=dict(
        id=task_id,
        status=job.status,
        retval=result,
    ))
