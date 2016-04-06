# -*- coding: utf-8 -*-
"""
rio.app
~~~~~~~~~

Implement of rio app factory based on Flask.
"""

from os import environ
from flask import Flask

from .conf import configure_app

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
    app.config_from_envvar = app.config.from_envvar
    app.config_from_object = app.config.from_object
    configure_app(app)
    register_blueprints(app)
    return app
