# -*- coding: utf-8 -*-
"""
rio.blueprints.dashboard
~~~~~~~~~~~~~~~~~~~~~~~~

TODO
"""

import json

from slugify import slugify
from flask import Blueprint
from flask import jsonify
from flask_wtf import Form
from wtforms import StringField
from wtforms import SelectField
from wtforms.validators import DataRequired
from wtforms.validators import ValidationError
from wtforms.validators import Length
from wtforms.validators import URL
from rio.utils.user import get_current_user_id
from rio.utils.user import login_required
from rio.utils.slugify import slugify
from rio.utils.token import password_generator
from rio.models  import add_instance
from rio.models  import delete_instance
from rio.models  import get_data_or_404

bp = Blueprint('dashboard', __name__)

class NewProjectForm(Form):
    name = StringField('Name', validators=[DataRequired(), Length(max=64)])

class ConfirmDeleteProjectForm(Form):
    name = StringField('Name', validators=[DataRequired(), Length(max=64)])

class NewSenderForm(Form):
    slug = StringField('slug', validators=[DataRequired(), Length(max=64)])

class NewActionForm(Form):
    slug = StringField('slug', validators=[DataRequired(), Length(max=64)])
    description = StringField('description', validators=[DataRequired(), Length(max=255)])

class NewWebhookForm(Form):
    method = SelectField('Method', choices=['GET', 'POST'])
    url = StringField('URL', validators=[DataRequired(), URL(), Length(max=1024)])
    headers = StringField('Headers', validators=[Length(max=2048)])

    def validate_headers(self, field):
        try:
            json.loads(field.data)
        except ValueError:
            raise ValidationError('Invalid JSON data.')

@bp.errorhandler(404)
def handle_not_found(exception):
    return jsonify(message='not found'), 404

@bp.route('/projects/new', methods=['POST'])
@login_required
def new_project():
    """New Project."""
    form = NewProjectForm()
    if not form.validate_on_submit():
        return jsonify(errors=form.errors), 400

    data = form.data
    data['slug'] = slugify(data['name'])
    data['owner_id'] = get_current_user_id()

    id = add_instance('project', **data)

    if not id:
        return jsonify(errors={'name': ['duplicated slug.']}), 400

    project = get_data_or_404('project', id)

    return jsonify(**project)


@bp.route('/projects/<int:project_id>', methods=['DELETE'])
@login_required
def delete_project(project_id):
    """Delete Project."""
    project = get_data_or_404('project', project_id)

    if project['owner_id'] != get_current_user_id():
        return jsonify(message='forbidden'), 403

    delete_instance('project', project_id)

    return jsonify({})


@bp.route('/projects/<int:project_id>/senders', methods=['POST'])
@login_required
def new_sender(project_id):
    """Add sender."""

    project = get_data_or_404('project', project_id)

    if project['owner_id'] != get_current_user_id():
        return jsonify(message='forbidden'), 403

    form = NewSenderForm()
    if not form.validate_on_submit():
        return jsonify(errors=form.errors), 400

    data = form.data
    data['project_id'] = project_id
    data['token'] = password_generator(40)

    id = add_instance('sender', **data)

    if not id:
        return jsonify(errors={'name': ['duplicated slug.']}), 400

    sender = get_data_or_404('sender', id, 'sensitive')

    return jsonify(**sender)


@bp.route('/senders/<int:sender_id>', methods=['DELETE'])
@login_required
def delete_sender(sender_id):
    sender = get_data_or_404('sender', sender_id)
    project = get_data_or_404('project', sender['project_id'])

    if project['owner_id'] != get_current_user_id():
        return jsonify(message='forbidden'), 403

    delete_instance('sender', sender['id'])
    return jsonify({})


@bp.route('/projects/<int:project_id>/actions', methods=['POST'])
@login_required
def new_action(project_id):
    """Add action."""

    project = get_data_or_404('project', project_id)

    if project['owner_id'] != get_current_user_id():
        return jsonify(message='forbidden'), 403

    form = NewActionForm()
    if not form.validate_on_submit():
        return jsonify(errors=form.errors), 400

    data = form.data
    data['project_id'] = project_id

    id = add_instance('action', **data)

    if not id:
        return jsonify(errors={'name': ['duplicated slug.']}), 400

    action = get_data_or_404('action', id)

    return jsonify(**action)


@bp.route('/actions/<int:action_id>', methods=['DELETE'])
@login_required
def delete_action(action_id):
    """Delete action."""
    action = get_data_or_404('action', action_id)
    project = get_data_or_404('project', action['project_id'])

    if project['owner_id'] != get_current_user_id():
        return jsonify(message='forbidden'), 403

    delete_instance('sender', action['id'])

    return jsonify({})


@bp.route('/actions/<int:action_id>/webhooks', methods=['POST'])
@login_required
def add_webhook(action_id):
    action = get_data_or_404('action', action_id)
    project = get_data_or_404('project', action['project_id'])

    if project['owner_id'] != get_current_user_id():
        return jsonify(message='forbidden'), 403

    form = NewWebhookForm()
    if not form.validate_on_submit():
        return jsonify(errors=form.errors), 400

    data = form.data
    data['action_id'] = action_id

    id = add_instance('webhook', **data)

    if not id:
        return jsonify(errors={'name': ['duplicated definition.']}), 400

    webhook = get_data_or_404('webhook', id)

    return jsonify(**webhook)


@bp.route('/webhooks/<int:webhook_id>', methods=['DELETE'])
@login_required
def delete_webhook(webhook_id):
    """Delete webhook."""
    webhook = get_data_or_404('webhook', webhook_id)
    action = get_data_or_404('action', webhook['action_id'])
    project = get_data_or_404('project', action['project_id'])

    if project['owner_id'] != get_current_user_id():
        return jsonify(message='forbidden'), 403

    delete_instance('webhook', action['id'])

    return jsonify({})


@bp.route('/projects/<int:project_id>/transfer', methods=['POST'])
def transfer_project(project_id):
    pass
