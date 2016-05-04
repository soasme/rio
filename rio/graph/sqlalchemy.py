# -*- coding: utf-8 -*-
"""
rio.graph.sqlalchemy
~~~~~~~~~~~~~~~~~~~~
"""

from __future__ import absolute_import

from rio.core import cache
from rio.models import get_data_by_slug
from .base import Base
from .errors import MissingSender
from .errors import MissingProject
from .errors import MissingAction
from .errors import NotAllowed
from .errors import WrongSenderSecret

class SQLAlchemy(Base):

    @property
    def project(self):
        return cache.run(get_data_by_slug,
                         model='project',
                         slug=self.project_slug,
                         kind='simple')

    def get_action(self, action_slug):
        project = self.project

        if not project:
            raise MissingProject

        action = cache.run(get_data_by_slug,
                           model='action',
                           slug=action_slug,
                           kind='full',
                           project_id=project['id'])

        if not action:
            raise MissingAction

        # assert action belongs to a project
        if action['project']['slug'] != project['slug']:
            raise NotAllowed

        return action

    def verify_sender(self, username, password):
        project = self.project

        if not project:
            raise MissingProject

        # assert sender
        sender = cache.run(get_data_by_slug,
                           model='sender',
                           slug=username,
                           kind='sensitive',
                           project_id=project['id'],)

        if not sender:
            raise MissingSender

        # assert sender token
        if sender['token'] != password:
            raise WrongSenderSecret

        return True
