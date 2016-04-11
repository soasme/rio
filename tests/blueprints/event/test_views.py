# -*- coding: utf-8 -*-

import json

import pytest
from flask import url_for


def test_emit_topic_to_undefined_project_slug(client, sender_basic_token, topic):
    url = url_for('event.emit_topic', project_slug='non-exist', topic_slug=topic.slug)
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    resp = client.get(url, headers=headers)
    assert resp.status_code == 404

def test_emit_topic_to_undefined_topic_slug(client, sender_basic_token, project):
    url = url_for('event.emit_topic', project_slug=project.slug, topic_slug='non-exist')
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
    url = url_for('event.emit_topic', project_slug=project.slug, topic_slug=topic.slug)
    resp = client.get(url)
    assert resp.status_code == 401

def test_emit_topic_success(client, project, topic, webhook, sender_basic_token):
    url = url_for('event.emit_topic', project_slug=project.slug, topic_slug=topic.slug)
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    resp = client.get(url, headers=headers)
    assert resp.status_code == 200
    assert json.loads(resp.data)['tasks']

@pytest.mark.xfail
def test_emit_topic_should_incr_metrics(client):
    raise NotImplementedError

def test_emit_topic_should_track_failure_webhook(
        client, project, topic, webhook, sender_basic_token):
    url = url_for('event.emit_topic', project_slug=project.slug, topic_slug=topic.slug)
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    resp = client.get(url, headers=headers)

    event_id = resp.headers.get('X-RIO-EVENT-ID')
    url = url_for('event.get_event', project_slug=project.slug, event_id=event_id)
    resp = client.get(url, headers=headers)
    data = json.loads(resp.data)
    assert data['status'] == 'RAN'
    assert data['payload'] == {}
    assert data['tasks'][0]['status'] == 'FAILURE'
    assert data['tasks'][0]['method'] == webhook.method
    assert data['tasks'][0]['url'] == webhook.url

@pytest.mark.xfail
def test_emit_topic_should_ignore_success_webhook(
        client, project, topic, webhook, sender_basic_token):
    url = url_for('event.emit_topic', project_slug=project.slug, topic_slug=topic.slug)
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    resp = client.get(url, headers=headers)

    event_id = resp.headers.get('X-RIO-EVENT-ID')
    url = url_for('event.get_event', project_slug=project.slug, event_id=event_id)
    resp = client.get(url, headers=headers)
    assert resp.status_code == 404
