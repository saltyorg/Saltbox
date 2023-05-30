#!/bin/bash
printf '' > /srv/git/saltbox/ansible-update.log
{
    python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade --root-user-action=ignore virtualenv
    /srv/ansible/venv/bin/python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade pip setuptools wheel
    /srv/ansible/venv/bin/python3 -m pip install --no-cache-dir --disable-pip-version-check --upgrade ansible">=8.0.0,<9.0.0"
    cp /srv/ansible/venv/bin/ansible* /usr/local/bin/
} >> /srv/git/saltbox/ansible-update.log 2>&1

returnValue=$?

if [ $returnValue -ne 0 ]; then
    echo "An Error occurred check '/srv/git/saltbox/ansible-update.log' for more information."
fi

exit $returnValue
