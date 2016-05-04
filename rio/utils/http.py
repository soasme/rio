# -*- coding: utf-8 -*-
"""
rio.utils.http
~~~~~~~~~~~~~~

"""
import warnings
from urllib import urlencode

import six
import requests
from requests.exceptions import SSLError
from flask import current_app

from rio.core import celery
from rio.signals import webhook_ran
# In case SSL is unavailable (light builds) we can't import this here.
try:
    from OpenSSL.SSL import ZeroReturnError
except ImportError:
    class ZeroReturnError(Exception):
        pass


def get_user_agent():
    return 'rio/%s' % current_app.config.get('RIO_VERSION')

def build_session():
    """
    Rio requests session will attach rio identity in header.
    """
    session = requests.Session()
    session.headers.update({'User-Agent': get_user_agent()})
    return session


def raven_context(url, method=None, params=None, data=None, json=None,
                  headers=None, allow_redirects=False, timeout=30,
                  verify_ssl=True, user_agent=None):
    headers = {
        'User-Agent': get_user_agent(),
    }

    if json:
        headers.setdefault('Content-Type', 'application/json')

    if params:
        query_string = urlencode(params)
    else:
        query_string = None

    if json:
        data = json

    if not method:
        method = 'POST' if (data or json) else 'GET'

    return {
        'method': method,
        'url': url,
        'query_string': query_string,
        'data': data,
        'headers': headers,
    }


def urlopen(url, method=None, params=None, data=None, json=None,
            headers=None, allow_redirects=False, timeout=30,
            verify_ssl=True, user_agent=None):
    """
    A slightly safer version of ``urlib2.urlopen`` which prevents redirection
    and ensures the URL isn't attempting to hit a blacklisted IP range.
    """
    if user_agent is not None:
        warnings.warn('user_agent is no longer used with safe_urlopen')

    session = build_session()

    kwargs = {}

    if json:
        kwargs['json'] = json
        if not headers:
            headers = {}
        headers.setdefault('Content-Type', 'application/json')

    if data:
        kwargs['data'] = data

    if params:
        kwargs['params'] = params

    if headers:
        kwargs['headers'] = headers

    if method is None:
        method = 'POST' if (data or json) else 'GET'

    try:
        response = session.request(
            method=method,
            url=url,
            allow_redirects=allow_redirects,
            timeout=timeout,
            verify=verify_ssl,
            **kwargs
        )
    # Our version of requests does not transform ZeroReturnError into an
    # appropriately generically catchable exception
    except ZeroReturnError as exc:
        import sys
        exc_tb = sys.exc_info()[2]
        six.reraise(SSLError, exc, exc_tb)
        del exc_tb

    # requests' attempts to use chardet internally when no encoding is found
    # and we want to avoid that slow behavior
    if not response.encoding:
        response.encoding = 'utf-8'

    return response


def urlread(response):
    return response.content


def is_success_response(response):
    return 200 <= response.status_code < 300

def is_failure_response(response):
    return 500 <= response.status_code < 600

def is_invalid_response(response):
    return 400 <= response.status_code < 500

class FailureWebhookError(Exception):
    pass

class InvalidResponseError(FailureWebhookError):
    """The remote server gave an invalid response."""


class RemoteExecuteError(FailureWebhookError):
    """The remote task gave a custom error."""


class UnknownStatusError(FailureWebhookError):
    """The remote server gave an unknown status."""


def extract_response(raw_response):
    """Extract requests response object.

    only extract those status_code in [200, 300).

    :param raw_response: a requests.Resposne object.
    :return: content of response.
    """
    data = urlread(raw_response)

    if is_success_response(raw_response):
        return data
    elif is_failure_response(raw_response):
        raise RemoteExecuteError(data)
    elif is_invalid_response(raw_response):
        raise InvalidResponseError(data)
    else:
        raise UnknownStatusError(data)

def dispatch_webhook_request(url=None, method='GET', params=None,
                             json=None, data=None, headers=None, timeout=5):
    """Task dispatching to an URL.

    :param url: The URL location of the HTTP callback task.
    :param method: Method to use when dispatching the callback. Usually
        `GET` or `POST`.
    :param params: Keyword arguments to pass on to the HTTP callback.
    :param json: JSON as body to pass on to the POST HTTP callback.
    :param headers: HTTP headers applied to callback.
    """
    if method == 'GET':
        resp = urlopen(url, method, params=params, headers=headers)
    elif method in ('POST', 'DELETE', 'PUT'):
        resp = urlopen(url, method, json=json, data=data, headers=headers)
    else:
        raise NotImplementedError

    return extract_response(resp)
