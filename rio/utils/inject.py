# -*- coding: utf-8 -*-
"""
rio.utils.inject
~~~~~~~~~~~~~~~~
"""

from jinja2 import Template
import re

ENV_PLACEHODLER = re.compile(r'{{\s?(\w+)\s?}}')

def format_template(string='', env=None):
    """
    Format data with given env.

    Example:

        >>> format_template('http://{{ SERVICE_HOST }}/webhook/exec', {'SERVICE_HOST': 'app'})
        http://app/webhook/exec

    :param data: string with placeholder in it.
    :param env: a dictionary.
    :return: return formatted string.
    `format_config` will try to format strings contained env placeholder `{{ ENV_KEY }}`.
    If `ENV_KEY` does not exist in env, then this function will trhow assertion error.
    """
    env = env or {}
    return Template(string).render(**env)
