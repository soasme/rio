# -*- coding: utf-8 -*-

from slugify import slugify
from flask import Blueprint
from flask import jsonify
from flask_wtf import Form
from wtforms import StringField
from wtforms.validators import DataRequired
from wtforms.validators import ValidationError
from wtforms.validators import Length
from rio.utils.user import get_current_user_id
from rio.utils.user import login_required
from rio.utils.slugify import slugify
from rio.models  import add_instance
from rio.models  import get_data_or_404

bp = Blueprint('dashboard', __name__)

class NewProjectForm(Form):
    name = StringField('Name', validators=[DataRequired(), Length(max=64)])

class ConfirmDeleteProjectForm(Form):
    name = StringField('Name', validators=[DataRequired(), Length(max=64)])

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
    project = get_data_or_404('project', project_id)

    if project['owner_id'] != get_current_user_id():
        return jsonify(message='forbidden'), 403

    # TODO: implement delete_project
    task = delete_project.delay(project_id)

    return jsonify()

@bp.route('/projects/<int:project_id>/transfer', methods=['POST'])
def transfer_project(project_id):
    pass
