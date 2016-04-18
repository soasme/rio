# -*- coding: utf-8 -*-
"""
rio.models.utils
~~~~~~~~~~~~~~~~
"""

from uuid import UUID
from time import mktime
from datetime import datetime

from werkzeug.utils import import_string
from flask import abort
from sqlalchemy.exc import IntegrityError

from rio.core import db

def _formatted(value):
    if isinstance(value, (int, str, long, )):
        return value
    if isinstance(value, datetime):
        return int(mktime(value.timetuple()))
    return value


def _turn_row_to_dict(row):
    return {
        column.name: _formatted(getattr(row, column.name))
        for column in row.__table__.columns
    }


def get_model(model):
    """Get a model class by model name.

    if model does not exist, this function will throw ImportError.

    :param model: a string of model name.
    :return: a SQLAlchemy Model.
    """
    return import_string('rio.models.%s.%s' % (model.lower(), model.capitalize()))


def ins2dict(ins, kind=''):
    """Turn a SQLAlchemy Model instance to dict.

    :param ins: a SQLAlchemy instance.
    :param kind: specify which kind of dict tranformer should be called.
    :return: dict, instance data.

    If model has defined `to_xxx_dict`, then ins2dict(ins, 'xxx') will
    call `model.to_xxx_dict()`. Default kind is ''.

    If model not defined 'to_dict', then ins2dict will transform according
    by model column definition.
    """
    if kind and hasattr(ins, 'to_%s_dict' % kind):
        return getattr(ins, 'to_%s_dict' % kind)()
    elif hasattr(ins, 'to_dict'):
        return getattr(ins, 'to_dict')()
    else:
        return _turn_row_to_dict(ins)


def get_instance(model, instance_id):
    """Get an instance by id.

    :param model: a string, model name in rio.models
    :param id: an integer, instance id.
    :return: None or a SQLAlchemy Model instance.
    """
    try:
        model = get_model(model)
    except ImportError:
        return None

    return model.query.get(instance_id)


def get_data_or_404(model, instance_id, kind=''):
    """Get instance data by id.

    :param model: a string, model name in rio.models
    :param id: an integer, instance id.
    :param kind: a string specified which kind of dict tranformer should be called.
    :return: data.
    """
    instance = get_instance(model, instance_id)

    if not instance:
        return abort(404)

    return ins2dict(instance, kind)


def get_instance_by_slug(model, slug, **kwargs):
    """Get an instance by slug.

    :param model: a string, model name in rio.models
    :param slug: a string used to query by `slug`. This requires there is a
                 slug field in model definition.
    :return: None or a SQLAlchemy Model instance.
    """
    try:
        model = get_model(model)
    except ImportError:
        return None

    query_params = dict(kwargs)
    query_params['slug'] = slug

    return model.query.filter_by(**query_params).first()


def get_data_by_slug_or_404(model, slug, kind='', **kwargs):
    """Get instance data by slug and kind. Raise 404 Not Found if there is no data.

    This function requires model has a `slug` column.

    :param model: a string, model name in rio.models
    :param slug: a string used to query by `slug`. This requires there is a
                 slug field in model definition.
    :param kind: a string specified which kind of dict tranformer should be called.
    :return: a dict.
    """

    instance = get_instance_by_slug(model, slug, **kwargs)

    if not instance:
        return abort(404)

    return ins2dict(instance, kind)


def get_instance_by_bin_uuid(model, bin_uuid):
    """Get an instance by binary uuid.

    :param model: a string, model name in rio.models.
    :param bin_uuid: a 16-bytes binary string.
    :return: None or a SQLAlchemy instance.
    """
    try:
        model = get_model(model)
    except ImportError:
        return None

    return model.query.filter_by(**{'bin_uuid': bin_uuid}).first()


def get_data_by_hex_uuid_or_404(model, hex_uuid, kind=''):
    """Get instance data by uuid and kind. Raise 404 Not Found if there is no data.

    This requires model has a `bin_uuid` column.

    :param model: a string, model name in rio.models
    :param hex_uuid: a hex uuid string in 24-bytes human-readable representation.
    :return: a dict.
    """
    uuid = UUID(hex_uuid)
    bin_uuid = uuid.get_bytes()

    instance = get_instance_by_bin_uuid(model, bin_uuid)

    if not instance:
        return abort(404)

    return ins2dict(instance, kind)


def add_instance(model, _commit=True, **kwargs):
    """Add instance to database.

    :param model: a string, model name in rio.models
    :param _commit: control whether commit data to database or not. Default True.
    :param \*\*kwargs: persisted data.
    :return: instance id.
    """
    try:
        model = get_model(model)
    except ImportError:
        return None

    instance = model(**kwargs)
    db.session.add(instance)

    try:
        if _commit:
            db.session.commit()
        else:
            db.session.flush()
        return instance.id
    except IntegrityError:
        db.session.rollback()
        return


def delete_instance(model, instance_id, _commit=True):
    """Delete instance.

    :param model: a string, model name in rio.models.
    :param instance_id: integer, instance id.
    :param _commit: control whether commit data to database or not. Default True.
    """
    try:
        model = get_model(model)
    except ImportError:
        return

    instance = model.query.get(instance_id)
    if not instance:
        return

    db.session.delete(instance)
    try:
        if _commit:
            db.session.commit()
        else:
            db.session.flush()
    except Exception as exception:
        db.session.rollback()
        raise exception
