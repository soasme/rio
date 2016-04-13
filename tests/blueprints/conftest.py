# -*- coding: utf-8 -*-

from base64 import b64encode

from flask import jsonify
from pytest import fixture

@fixture
def owner(session):
    from rio.models.user import User
    user = User(
        username='owner',
        email='owner@example.org',
    )
    session.add(user)
    session.commit()
    return user

@fixture
def project(session, owner):
    from rio.models.project import Project
    project_ = Project(
        owner_id=owner.id,
        slug='example-project',
        name='Example Project',
    )
    session.add(project_)
    session.commit()
    return project_

@fixture
def sender(session, project):
    from rio.models.sender import Sender
    sender_ = Sender(
        project_id=project.id,
        slug='example-sender',
        token='*',
    )
    session.add(sender_)
    session.commit()
    return sender_

@fixture
def sender_basic_token(sender):
    return b64encode('%s:%s' % (sender.slug, sender.token))

@fixture
def action(session, project):
    from rio.models.action import Action
    action_ = Action(
        project_id=project.id,
        slug='example-action',
    )
    session.add(action_)
    session.commit()
    return action_

@fixture
def builtin_webhook(app):
    @app.route('/webhook/success-example')
    def get():
        return jsonify(status='success', retval=0)

@fixture
def webhook(session, action):
    from rio.models.webhook import Webhook
    webhook_ = Webhook(
        action_id=action.id,
        method_id=Webhook.Method.GET,
        url='http://example.org'
    )
    session.add(webhook_)
    session.commit()
    return webhook_
