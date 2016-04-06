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


def get_webhook(url, payload):
    """Executing Celery `GET` URL task.

    :param url: string, url that will be requested via `GET` method.
    :param payload: dict, payload will be transformed to query string.
    """
    return URL(url, app=celery, dispatcher=None).get_async(**payload)


def post_webhook(url, payload):
    """Executing Celery `POST` URL task.

    :param url: string, url that will be requested via `POST` method.
    :param payload: dict, payload will be transformed to form data.
    """
    return URL(url, app=celery, dispatcher=None).post_async(**payload)
