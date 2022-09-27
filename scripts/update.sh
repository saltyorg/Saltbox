#!/bin/bash
printf '' > /srv/git/saltbox/ansible-update.log
python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade ansible">=6.0.0,<7.0.0" 2>&1 | tee -a /srv/git/saltbox/ansible-update.log
