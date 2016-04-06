# -*- coding: utf-8 -*-
"""
rio.tasks
~~~~~~~~~~

Implement of rio tasks based on celery.
"""

from os import environ

from celery import Celery

from .conf import configure_app

def register_tasks(app):
    """Register tasks to application.
    """
    pass


def create_app():
    """Celery application factory function."""
    app = Celery('rio')
    configure_app(app)
    register_tasks(app)
    return app
