# -*- coding: utf-8 -*-

from pytest import fixture, mark

from rio.core import cache

@mark.xfail
def test_cache_function_call(app):
    def hello_world(k1, k2):
        return {'k1': 'v1', 'k2': 'v2'}
    assert not cache.get(':hello_world:k1:v1:k2:v2')
    assert cache.run(hello_world, k1='v1', k2='v2')
    assert cache.get(':hello_world:k1:v1:k2:v2') == {'k1': 'v1', 'k2': 'v2'}

def test_clean_cache_function_call(app):
    def hello_world(k1, k2):
        return {'k1': 'v1', 'k2': 'v2'}
    cache.run(hello_world, k1='v1', k2='v2')
    cache.clean(hello_world, k1='v1', k2='v2')
    assert not cache.get(':hello_world:k1:v1:k2:v2')
