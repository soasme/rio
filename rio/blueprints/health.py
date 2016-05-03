# -*- coding: utf-8 -*-

from flask import Blueprint

bp = Blueprint('health', __name__)

@bp.route('/')
def index():
    return 'OK'
