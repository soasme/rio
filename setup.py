#!/usr/bin/env python
# -*- coding: utf-8 -*-

from setuptools import setup, find_packages

with open('README.rst') as readme_file:
    readme = readme_file.read()

with open('HISTORY.rst') as history_file:
    history = history_file.read()

requirements = [
    'flask==0.10.1',
    'flask-sqlalchemy==2.1',
    'flask-migrate==1.8.0',
    'Flask-Celery-Helper==1.1.0',
    'flask-user==0.6.8',
    'celery==3.1.23',
    'redis==2.10.5',
]

test_requirements = [
    # TODO: put package test requirements here
    'pytest',
]

setup(
    name='rio',
    version='0.1.0',
    description="RESTful event dispatcher based on celery.",
    long_description=readme + '\n\n' + history,
    author="Ju Lin",
    author_email='soasme@gmail.com',
    url='https://github.com/soasme/rio',
    packages=['rio'],
    include_package_data=True,
    install_requires=requirements,
    license="MIT",
    zip_safe=False,
    keywords='rio',
    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: ISC License (ISCL)',
        'Natural Language :: English',
        "Programming Language :: Python :: 2",
        'Programming Language :: Python :: 2.6',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
    ],
    test_suite='tests',
    tests_require=test_requirements
)
