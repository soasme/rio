# -*- coding: utf-8 -*-
"""
rio.setup
~~~~~~~~~
"""

from os import environ
from os import path

from .core import db
from .core import celery
from .core import redis
from .core import cache
from .core import sentry
from .core import migrate
from .core import user_manager

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

    from .blueprints.event import bp as event_bp
    app.register_blueprint(event_bp, url_prefix='/event')

    from .blueprints.api_1 import bp as api_1_bp
    app.register_blueprint(api_1_bp, url_prefix='/api/1')

    from .blueprints.dashboard import bp as dashboard_bp
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')

    from .blueprints.health import bp as health_bp
    app.register_blueprint(health_bp, url_prefix='/health')


def setup_user_manager(app):
    """Setup flask-user manager."""

    from flask_user import SQLAlchemyAdapter

    from rio.models import User

    init = dict(
        db_adapter=SQLAlchemyAdapter(db, User),
    )
    user_manager.init_app(app, **init)


def setup_migrate(app):
    """Setup flask-migrate."""
    directory = path.join(path.dirname(__file__), 'migrations')
    migrate.init_app(app, db, directory=directory)


def init_core(app):
    """Init core objects."""
    from rio import models # noqa
    db.init_app(app)
    celery.init_app(app)
    redis.init_app(app)
    cache.init_app(app)
    sentry.init_app(app)
    setup_migrate(app)
    setup_user_manager(app)
