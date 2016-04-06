# -*- coding: utf-8 -*-

from os import environ

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
        app.config_from_envvar(environ.get('RIO_SETTINGS'))
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
