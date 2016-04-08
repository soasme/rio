# -*- coding: utf-8 -*-
"""
rio.app
~~~~~~~~~

Implement of rio app factory based on Flask.
"""

from os import environ
from flask import Flask

from .core import db
from .core import celery

def configure_app(app):
    """Configure Flask/Celery application.

    * Rio will find environment variable `RIO_SETTINGS` first::

        $ export RIO_SETTINGS=/path/to/settings.cfg
        $ rio worker

    * If `RIO_SETTINGS` is missing, Rio will try to load configuration
      module in `rio.settings` according to another environment
      variable `RIO_ENV`. Default load `rio.settings.dev`.

        $ export RIO_ENV=prod
        $ rio worker
    """
    app.config_from_object('rio.settings.default')

    if environ.get('RIO_SETTINGS'):
        app.config_from_envvar('RIO_SETTINGS')
        return

    config_map = {
        'dev': 'rio.settings.dev',
        'stag': 'rio.settings.stag',
        'prod': 'rio.settings.prod',
        'test': 'rio.settings.test',
    }

    rio_env = environ.get('RIO_ENV', 'dev')
    config = config_map.get(rio_env, config_map['dev'])
    app.config_from_object(config)


def register_blueprints(app):
    """Register blueprints to application.

    Currently, Rio registered:

    * /api/1
    * /dashboard
    """
    from .blueprints.api_v1 import bp as api_v1_bp
    app.register_blueprint(api_v1_bp, url_prefix='/api/1')

    from .blueprints.dashboard import bp as dashboard_bp
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')

def init_core(app):
    """Init core objects."""
    from rio import models # noqa
    db.init_app(app)
    celery.init_app(app)

def create_app():
    """Flask application factory function."""
    app = Flask(__name__)
    app.config_from_envvar = app.config.from_envvar
    app.config_from_object = app.config.from_object
    configure_app(app)
    init_core(app)
    register_blueprints(app)
    return app
