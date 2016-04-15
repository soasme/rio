# -*- coding: utf-8 -*-

from flask_user import current_user, login_required

def get_current_user_id():
    return current_user.id
