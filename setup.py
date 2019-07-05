import os
import sys

from setuptools import find_packages, setup


here = os.path.abspath(os.path.dirname(__file__))
README = open(os.path.join(here, 'README.rst')).read()
VERSION = '0.5.4'


if sys.argv[-1] == 'publish':
    os.system('python setup.py sdist bdist_wheel upload')
    print "You should also consider tagging a release:"
    print "  git tag -a %s -m 'version %s'" % (VERSION, VERSION)
    print "  git push --tags"
    sys.exit()


setup(
    name='nose-picker',
    version=VERSION,
    description='nose plugin that picks a subset of your unit tests',
    classifiers=[
        'License :: OSI Approved :: BSD License',
        'Programming Language :: Python :: 2.6',
        'Programming Language :: Python :: 2.7',
    ],
    long_description=README,
    url='https://github.com/eventbrite/nose-picker',
    license='BSD',
    packages=find_packages('src'),
    package_dir={'': 'src'},
    include_package_data=True,
    zip_safe=True,
    entry_points={
        'nose.plugins.0.10': ['picker = picker.nose_plugin:NosePicker'],
    },
)
