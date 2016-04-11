# -*- coding: utf-8 -*-
"""
rio.tasks
~~~~~~~~~~

Implement of rio tasks based on celery.
"""

from time import time

from celery import chord
from requests import ConnectionError

from rio.core import celery
from rio.utils.http import dispatch_webhook_request
from rio.utils.http import FailureWebhookError



@celery.task()
def call_webhook(event, webhook, payload):
    """Build request from event,webhook,payoad and parse response."""
    started_at = time()
    request = {'url': webhook['url'], 'method': webhook['method']}
    request['headers'] = {
        'X-RIO-EVENT': 'uuid=%s,project=%s,topic=%s' % (
            str(event['uuid']),
            event['project'],
            event['topic']
        ),
    }
    if webhook['method'] == 'GET':
        request['params'] = payload
    else:
        request['json'] = payload

    try:
        content = dispatch_webhook_request(**request)
    except (FailureWebhookError, ConnectionError) as exception:
        # TODO: propagate to sentry if configured.
        return dict(
            parent=str(event['uuid']),
            error=exception.message,
            started_at=started_at,
            ended_at=time(),
        )
    return dict(
        parent=str(event['uuid']),
        content=content,
        started_at=started_at,
        ended_at=time()
    )


@celery.task()
def merge_webhooks_runset(runset):
    """Make some statistics on the run set.
    """
    min_started_at = min([w['started_at'] for w in runset])
    max_ended_at = max([w['ended_at'] for w in runset])
    ellapse = max_ended_at - min_started_at
    errors_count = sum(1 for w in runset if 'error' in w)
    total_count = len(runset)
    return dict(
        ellapse=ellapse,
        errors_count=errors_count,
        total_count=total_count,
    )


def exec_event(event, webhooks, payload):
    """Execute event.

    Merge webhooks run set to do some stats after all
    of the webhooks been responded successfully.

    Error webhook will be propagated. Note that other webhook
    calls will still execute.
    """
    calls = (
        call_webhook.s(event, webhook, payload)
        for webhook in webhooks
    )
    callback = merge_webhooks_runset.s()

    call_promise = chord(calls)
    return call_promise(callback)
