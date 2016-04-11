# -*- coding: utf-8 -*-
"""
rio.tasks
~~~~~~~~~~

Implement of rio tasks based on celery.
"""

from time import time

from celery import chord

from rio.core import celery
from rio.utils.http import dispatch_webhook_request



@celery.task()
def call_webhook(event, webhook, payload):
    started_at = time()
    request = {'url': webhook['url'], 'method': webhook['method']}
    request['headers'] = {
        'X-RIO-EVENT': 'project=%s,topic=%s' % (
            str(event['uuid']),
            event['project'],
            event['topic']
        ),
    }
    if webhook['method'] == 'GET':
        request['params'] = payload
    else:
        request['json'] = payload

    content = dispatch_webhook_request(**request)
    ended_at = time()
    return dict(
        parent=event['uuid'],
        content=content,
        started_at=started_at,
        ended_at=ended_at
    )


@celery.task()
def merge_webhooks_runset(runset):
    min_started_at = min([w['started_at'] for w in runset])
    max_ended_at = max([w['ended_at'] for w in runset])
    ellapse = max_ended_at - min_started_at
    return dict(ellapse=ellapse)


@celery.task(bind=True)
def handle_event_exec_failed(self, uuid):
    result = self.app.AsyncResult(uuid)
    # TODO: build graph or send to sentry
    print 'Captured error: ', result.result, result.traceback


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
    errback = handle_event_exec_failed.s()
    callback = merge_webhooks_runset.subtask(link_error=errback)

    call_promise = chord(calls)
    return call_promise(callback)
