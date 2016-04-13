# -*- coding: utf-8 -*-
"""
rio.app
~~~~~~~~~

Implement of rio app factory based on Flask.
"""

from flask import Flask

from rio.setup import configure_app
from rio.setup import init_core
from rio.setup import register_blueprints

def create_app():
    """Flask application factory function."""
    app = Flask(__name__)
    app.config_from_envvar = app.config.from_envvar
    app.config_from_object = app.config.from_object
    configure_app(app)
    init_core(app)
    register_blueprints(app)
    return app
