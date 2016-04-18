# -*- coding: utf-8 -*-

import os
import re

import yaml
from flask_script import Command
from flask_script import Option
from flask import current_app

from rio.core import db
from rio.models import User
from rio.models import Project
from rio.models import Action
from rio.models import Webhook
from rio.models import Sender
from rio.utils.slugify import slugify


ENV_PLACEHODLER = re.compile(r'{{\s?(\w+)\s?}}')

def format_config(data, env):
    """
    Format data with given env.

    :param data: a string/integer/float/boolean/list/dict object.
    :param env: a dictionary.
    :return: return formatted data.

    `format_config` will try to format strings contained env placeholder `{{ ENV_KEY }}`.

    If `ENV_KEY` does not exist in env, then this function will trhow assertion error.
    """
    if isinstance(data, list):
        return [format_config(datum, env) for datum in data]
    elif isinstance(data, dict):
        return {
            format_config(key, env): format_config(value, env)
            for key, value in data.items()
        }
    elif isinstance(data, str):
        def replace(match):
            var_name = match.group(1)
            var = env.get(var_name)
            assert var, 'Variable %s undefined.' % var_name
            return var
        return ENV_PLACEHODLER.sub(replace, data)
    else:
        return data


class Environ(object):

    @staticmethod
    def get(key):
        return (
            current_app.config.get(key) or
            os.environ.get(key)
        )


class SyncProjectCommand(Command):
    """Sync project data.
    """

    option_list = (
        Option('--owner', required=True),
        Option('--project-data', required=True),
    )

    def run(self, owner, project_data):
        with open(project_data) as f:
            data = yaml.load(f.read())
            data = format_config(data, Environ)

        user = User.query.filter_by(username=owner).first()
        if not user:
            user = User(username=owner, email='')
            db.session.add(user)
            db.session.commit()
            print 'ADD USER', owner

        project = Project.query.filter_by(slug=data['project']).first()
        if not project:
            slug = slugify(data['project'])
            project = Project(owner_id=user.id, slug=slug, name=data['project'])
            db.session.add(project)
            db.session.commit()
            print 'ADD PROJECT', project.slug

        for _sender in data['senders']:
            sender = Sender.query.filter_by(project_id=project.id, slug=_sender['slug']).first()
            if not sender:
                sender = Sender(project_id=project.id, **_sender)
                db.session.add(sender)
                db.session.commit()
                print 'ADD SENDER', _sender['slug']
            elif sender.token != _sender['token']:
                sender.token = _sender['token']
                db.session.add(sender)
                db.session.commit()
                print 'UPDATE SENDER', _sender['slug']

        sender_slugs = {sender['slug'] for sender in data['senders']}
        for _sender in Sender.query.all():
            if _sender.slug not in sender_slugs:
                db.session.delete(_sender)
                db.session.commit()
                print 'DEL SENDER', _sender.slug

        for _action in data['actions']:
            action = Action.query.filter_by(slug=_action['slug']).first()
            if not action:
                action = Action(project_id=project.id, slug=_action['slug'])
                db.session.add(action)
                db.session.commit()
                print 'ADD ACTION', _action['slug']
            webhooks = _action['webhooks']
            for _webhook in webhooks:
                method, url = _webhook['method'], _webhook['url']
                headers = _webhook.get('headers') or {}
                webhook = Webhook.query_filter_by(method=method, url=url, action_id=action.id).first()
                if not webhook:
                    webhook = Webhook(action_id=action.id, url=url, method=method, headers=headers)
                    db.session.add(webhook)
                    db.session.commit()
                    print 'ADD WEBHOOK FOR', _action['slug'], method, url
                elif webhook.headers != headers:
                    webhook.headers = headers
                    db.session.add(webhook)
                    db.session.commit()
                    print 'UPDATE WEBHOOK FOR', _action['slug'], method, url
            _webhook_set = {(w['method'], w['url']) for w in webhooks}
            _webhooks = Webhook.query.filter_by(action_id=action.id).all()
            for _webhook in _webhooks:
                if (_webhook.method, _webhook.url) not in _webhook_set:
                    db.session.delete(_webhook)
                    db.session.commit()
                    print 'DEL WEBHOOK FOR', _action['slug'], _webhook.method, _webhook.url
        actions = {action['slug'] for action in data['actions']}
        for _action in Action.query.all():
            if _action.slug not in actions:
                for _webhook in Webhook.query.filter_by(action_id=_action.id).all():
                    db.session.delete(_webhook)
                    db.session.commit()
                    print 'DEL WEBHOOK FOR', _action.slug, _webhook.method, _webhook.url
                db.session.delete(_action)
                db.session.commit()
                print 'DEL ACTION', _action.slug
