# -*- coding: utf-8 -*-
"""
rio.graph.base
~~~~~~~~~~~~~~
"""

class Base(object):

    def __init__(self, project_slug):
        self.project_slug = project_slug

    @property
    def project(self):
        raise NotImplementedError

    def get_action(self, action_slug):
        raise NotImplementedError

    def verify_sender(self, username, password):
        raise NotImplementedError
