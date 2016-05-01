# -*- coding: utf-8 -*-
"""
rio.blueprints.event.controllers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

import json
import logging
from uuid import uuid4

from rio.core import cache
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
    project = cache.run(get_data_by_slug_or_404,
                        model='project',
                        slug=project_slug,
                        kind='simple')
    project_id = project['id']

    # assert sender
    sender = cache.run(get_data_by_slug_or_404,
                       model='sender',
                       slug=sender_name,
                       kind='sensitive',
                       project_id=project_id,)

    if not sender:
        raise MissingSender

    # assert sender token
    if sender['token'] != sender_secret:
        raise WrongSenderSecret

    action = cache.run(get_data_by_slug_or_404,
                       model='action',
                       slug=action_slug,
                       kind='full',
                       project_id=project_id)

    # assert action belongs to a project
    if action['project']['slug'] != project['slug']:
        raise NotAllowed

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
