# -*- coding: utf-8 -*-
"""
rio.tasks
~~~~~~~~~~

Implement of rio tasks based on celery.
"""

from time import time

from celery import chord
from requests import ConnectionError

from celery.utils.log import get_task_logger

from rio.core import celery
from rio.core import sentry
from rio.utils.http import dispatch_webhook_request
from rio.utils.http import raven_context
from rio.utils.http import FailureWebhookError
from rio.signals import webhook_ran


logger = get_task_logger(__name__)


def _build_request_for_calling_webhook(event, webhook, payload):
    event_identity = 'uuid=%s,project=%s,action=%s' % (
        str(event['uuid']), event['project'], event['action']
    )

    request = {
        'url': webhook['url'],
        'method': webhook['method'],
        'headers': {
            'X-RIO-EVENT': event_identity,
        }
    }


    if webhook['method'] == 'GET':
        request['params'] = payload
    elif webhook['headers'].get('Content-Type') == 'application/json':
        request['json'] = payload
    else:
        request['data'] = payload

    return request


@celery.task()
def call_webhook(event, webhook, payload):
    """Build request from event,webhook,payoad and parse response."""
    started_at = time()

    logger.info('REQUEST %(uuid)s %(method)s %(url)s %(payload)s' % dict(
        uuid=str(event['uuid']),
        url=webhook['url'],
        method=webhook['method'],
        payload=payload,
    ))

    request = _build_request_for_calling_webhook(event, webhook, payload)

    try:
        content = dispatch_webhook_request(**request)

        logger.debug('RESPONSE %(uuid)s %(method)s %(url)s %(data)s' % dict(
            uuid=str(event['uuid']),
            url=webhook['url'],
            method=webhook['method'],
            data=content,
        ))

        data = dict(
            parent=str(event['uuid']),
            content=content,
            started_at=started_at,
            ended_at=time()
        )
    except (FailureWebhookError, ConnectionError) as exception:
        if sentry.client:
            http_context = raven_context(**request)
            sentry.captureException(data={'request': http_context})

        logger.error('RESPONSE %(uuid)s %(method)s %(url)s %(error)s' % dict(
            uuid=str(event['uuid']),
            method=webhook['method'],
            url=webhook['url'],
            error=exception.message,))

        data = dict(
            parent=str(event['uuid']),
            error=exception.message,
            started_at=started_at,
            ended_at=time(),
        )

    webhook_ran.send(None, data=data)

    return data


@celery.task()
def merge_webhooks_runset(runset):
    """Make some statistics on the run set.

    """
    min_started_at = min([w['started_at'] for w in runset])
    max_ended_at = max([w['ended_at'] for w in runset])
    ellapse = max_ended_at - min_started_at
    errors_count = sum(1 for w in runset if 'error' in w)
    total_count = len(runset)

    data = dict(
        ellapse=ellapse,
        errors_count=errors_count,
        total_count=total_count,
    )

    return data


def exec_event(event, webhooks, payload):
    """Execute event.

    Merge webhooks run set to do some stats after all
    of the webhooks been responded successfully.

    +---------+
    |webhook-1+--------------------+
    +---------+                    |
                                   |
    +---------+                    |
    |webhook-2+-------------+      |
    +---------+             +------+-----+
                            |merge runset+------>
    +---------+             +------+-----+
    |webhook-3+-------------+      |
    +---------+                    |
                                   |
    +---------+                    |
    |...      +--------------------+
    +---------+


    Error webhook will be propagated. Note that other webhook
    calls will still execute.
    """
    calls = (
        call_webhook.s(event, webhook, payload)
        for webhook in webhooks
    )
    callback = merge_webhooks_runset.s()

    call_promise = chord(calls)
    promise = call_promise(callback)
    return promise
