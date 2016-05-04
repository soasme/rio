# -*- coding: utf-8 -*-
"""
rio.graph.extension
~~~~~~~~~~~~~~~~~~~
"""

from flask import current_app

class Graph(object):

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        app.config.setdefault('GRAPH_BACKEND', 'directory')

    def get_project_graph(self, project_slug):
        backend = current_app.config.get('GRAPH_BACKEND')
        if backend == 'directory':
            from .directory import Directory
            return Directory(project_slug)
        elif backend == 'sqlalchemy':
            from .sqlalchemy import SQLAlchemy
            return SQLAlchemy(project_slug)
        else:
            raise NotImplementedError
