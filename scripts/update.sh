#!/bin/bash
printf '' > /srv/git/saltbox/ansible-update.log
python3 -m pip uninstall -y --no-cache-dir --disable-pip-version-check ansible ansible-base 1>>/srv/git/saltbox/ansible-update.log 2>>/srv/git/saltbox/ansible-update.log
python3 -m pip install --no-cache-dir --disable-pip-version-check ansible">=6.0.0,<7.0.0" 1>>/srv/git/saltbox/ansible-update.log 2>>/srv/git/saltbox/ansible-update.log
