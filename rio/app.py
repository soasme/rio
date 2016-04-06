# -*- coding: utf-8 -*-
"""
rio.app
~~~~~~~~~

Implement of rio app factory based on Flask.
"""

from os import environ
from flask import Flask

def configure_app(app):
    """Configure Flask application.

    * Rio will find environment variable `RIO_SETTINGS` first::

        $ export RIO_SETTINGS=/path/to/settings.cfg
        $ rio start

    * If `RIO_SETTINGS` is missing, Rio will try to load configuration
      module in `rio.settings` according to another environment
      variable `FLASK_ENV`. Default load `rio.settings.dev`.

        $ export FLASK_ENV=prod
        $ rio start
    """
    if environ.get('RIO_SETTINGS'):
        app.config.from_envvar(environ.get('RIO_SETTINGS'))
        return

    config_map = {
        'dev': 'rio.settings.dev',
        'stag': 'rio.settings.stag',
        'prod': 'rio.settings.prod',
        'test': 'rio.settings.test',
    }

    flask_env = environ.get('FLASK_ENV', 'dev')
    config = config_map.get(flask_env, config_map['dev'])
    app.config.from_object(config)


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

def create_app():
    """Flask application factory function."""
    app = Flask(__name__)
    configure_app(app)
    return app
