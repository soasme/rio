# -*- coding: utf-8 -*-

import json

import pytest
from flask import url_for


def test_emit_topic_to_undefined_project_slug(client, sender_basic_token, topic):
    url = url_for('api_v1.emit_topic', project_slug='non-exist', topic_slug=topic.slug)
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    resp = client.get(url, headers=headers)
    assert resp.status_code == 404

def test_emit_topic_to_undefined_topic_slug(client, sender_basic_token, project):
    url = url_for('api_v1.emit_topic', project_slug=project.slug, topic_slug='non-exist')
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    resp = client.get(url, headers=headers)
    assert resp.status_code == 404

@pytest.mark.xfail
def test_emit_topic_to_topic_slug_not_belongs_to_project(session):
    raise NotImplementedError

@pytest.mark.xfail
def test_emit_topic_that_sender_has_no_permission(session):
    raise NotImplementedError

def test_emit_topic_but_sender_has_not_logined(client, project, topic):
    url = url_for('api_v1.emit_topic', project_slug=project.slug, topic_slug=topic.slug)
    resp = client.get(url)
    assert resp.status_code == 401

def test_emit_topic_success(client, project, topic, webhook, sender_basic_token):
    url = url_for('api_v1.emit_topic', project_slug=project.slug, topic_slug=topic.slug)
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    resp = client.get(url, headers=headers)
    assert resp.status_code == 200
    assert json.loads(resp.data)['tasks']
