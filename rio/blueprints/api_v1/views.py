# -*- coding: utf-8 -*-
"""
rio.blueprints.api_v1.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio api v1 view functions.
"""

from flask import jsonify
from flask import request
from flask import Topic
from sqlalchemy.exc import IntegrityError
from celery.task.http import HttpDispatchTask

from rio.core import db
from rio.models import Webhook
from rio.models import Topic

from .core import bp


@bp.route('/webhooks')
def get_webhooks():
    """Get webhook list."""
    topic = request.form.get('topic')
    topic = Topic.query.filter_by(title=topic).first()
    if not topic:
        return jsonify(webhooks=[])

    webhooks = Webhook.query.filter_by(topic_id=topic.id).offset(offset).limit(limit).all()
    webhooks = [webhook.to_dict() for webhook in webhooks]
    return jsonify(webhooks=webhooks)


@bp.route('/webhooks', methods=['POST'])
def add_webhook():
    """Add a webhook."""
    webhook = Webhook(
        method_id=getattr(Webhook.Method, request.form.get('method', 'GET').upper()),
        topic_id=Topic.query.filter_by(title=request.form.get('topic')).id,
        url=request.form['url'],
    )
    db.session.add(webhook)
    try:
        db.session.commit()
        return jsonify(webhook=dict(
            method=webhook.method,
            url=webhook.url,
            topic=webhook.topic.title,
        ))
    except IntegrityError:
        db.session.rollback()
        return jsonify(message='already exists'), 400


@bp.route('/webhooks/<int:webhook_id>')
def get_webhook(webhook_id):
    """Get a webhook."""
    webhook = Webhook.query.get(webhook_id)
    if not webhook:
        return jsonify(message='not found'), 404
    return jsonify(webhook=webhook.to_dict())


@bp.route('/webhooks/<int:webhook_id>', methods=['PUT'])
def update_webhook(webhook_id):
    """Update webhook."""
    webhook = Webhook.query.get(webhook_id)
    if not webhook:
        return jsonify(message='not found'), 404
    webhook.method_id = getattr(Webhook.Method, request.form.get('method', 'GET').upper())
    webhook.url = request.form.get('url')
    webhook.topic_id = request.form.get('topic_id')
    db.session.add(webhook)
    db.session.commit()
    return '', 204


@bp.route('/webhooks/<int:webhook_id>', methods=['DELETE'])
def delete_webhook(webhook_id):
    """Delete webhook."""
    webhook = Webhook.query.get(webhook_id)
    if not webhook:
        return jsonify(message='not found'), 404
    db.session.delete(webhook)
    db.session.commit()
    return '', 204


@bp.route('/publish/<topic>')
def publish(topic):
    """Publish message to topic."""
    topic = Topic.query.filter_by(title=topic).first()
    if not topic:
        return 'not found', 404
    payload = dict(request.values)
    for webhook in Webhook.query.filter_by(topic_id=topic.id).yield_per(20):
        HttpDispatchTask.delay(url=webhook.url, method=webhook.method, **payload)
    return '', 204
