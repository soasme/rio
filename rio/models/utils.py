# -*- coding: utf-8 -*-
"""
rio.models.utils
~~~~~~~~~~~~~~~~
"""

from time import mktime
from datetime import datetime

from werkzeug.utils import import_string
from flask import abort


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


def get_instance_by_slug(model, slug):
    """Get a instance by slug.

    :param model: a string, model name in rio.models
    :param slug: a string used to query by `slug`. This requires there is a
                 slug field in model definition.
    :return: a SQLAlchemy Model instance.
    """
    try:
        model = import_string('rio.models.%s.%s' % (model.lower(), model.capitalize()))
    except ImportError:
        return None

    return model.query.filter_by(slug=slug).first()


def get_data_by_slug_or_404(model, slug, kind=''):
    """Get instance data by slug and kind. Raise 404 Not Found if there is no data.

    :param model: a string, model name in rio.models
    :param slug: a string used to query by `slug`. This requires there is a
                 slug field in model definition.
    :param kind: a string specified which kind of dict tranformer should be called.
    :return: a dict.
    """

    instance = get_instance_by_slug(model, slug)

    if not instance:
        return abort(404)

    return ins2dict(instance, kind)
