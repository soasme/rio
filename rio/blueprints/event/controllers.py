# -*- coding: utf-8 -*-
"""
rio.blueprints.event.controllers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import json
import logging
from uuid import uuid4

from rio.core import cache
from rio.core import graph
from rio.models import get_data_by_slug_or_404
from rio.tasks import exec_event

logger = logging.getLogger('rio.event')

class MissingSender(Exception):
    pass


class WrongSenderSecret(Exception):
    pass


class NotAllowed(Exception):
    pass


def emit_event(project_slug, action_slug, payload, sender_name, sender_secret):
    """Emit Event.

    :param project_slug: the slug of the project
    :param action_slug: the slug of the action
    :param payload: the payload that emit with action
    :param sender_name: name that identified the sender
    :parma sender_secret: secret string
    :return: dict with task_id and event_uuid

    raise MissingSender if sender does not exist
    raise WrongSenderSecret if sender_secret is wrong
    raise NotAllowed if sender is not allowed to emit action to project
    """
    project_graph = graph.get_project_graph(project_slug)

    project_graph.verify_sender(sender_name, sender_secret)
    action = project_graph.get_action(action_slug)
    project = project_graph.project

    # execute event
    event = {'uuid': uuid4(), 'project': project['slug'], 'action': action['slug']}

    res = exec_event(event, action['webhooks'], payload)

    logger.info('EMIT %s "%s" "%s" %s',
                 event['uuid'], project_slug, action_slug, json.dumps(payload))

    return dict(
        task=dict(
            id=res.id,
        ),
        event=dict(
            uuid=event['uuid'],
        ),
    )
