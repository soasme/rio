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
    return URL(url, app=celery, dispatcher=None).get_async(**payload)


def post_webhook(url, payload):
    return URL(url, app=celery, dispatcher=None).post_async(**payload)
