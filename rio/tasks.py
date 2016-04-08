# -*- coding: utf-8 -*-
"""
rio.tasks
~~~~~~~~~~

Implement of rio tasks based on celery.
"""

from os import environ

from celery import task
from celery.task.http import URL

from .core import celery

AVAILABLE_METHODS = {'GET', 'POST'}

def get_webhook(url, payload):
    """Executing Celery `GET` URL task.

    :param url: string, url that will be requested via `GET` method.
    :param payload: dict, payload will be transformed to query string.
    :return: AsyncResult object
    """
    return URL(url, app=celery, dispatcher=None).get_async(**payload)


def post_webhook(url, payload):
    """Executing Celery `POST` URL task.

    :param url: string, url that will be requested via `POST` method.
    :param payload: dict, payload will be transformed to form data.
    :return: AsyncResult object
    """
    return URL(url, app=celery, dispatcher=None).post_async(**payload)


def exec_webhook(webhook, payload):
    """Executing a webhook with payload.

    :param webhook: dict, contains `method` and `url`.
                    only `GET` and `POST` methods are allowed currently.
    :param url: string, the webhook url to be triggered.
    :return: dict, contains task runtime reference.
    """
    method, url = webhook['method'], webhook['url']

    if method not in AVAILABLE_METHODS:
        return

    if method == 'GET':
        runner = get_webhook
    elif method == 'POST':
        runner = post_webhook

    res = runner(url, payload)

    return {
        'task_id': res.id,
        'url': url,
        'method': method
    }
