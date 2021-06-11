#!/bin/bash
#########################################################################
# Title:         Cloudbox: Python-PlexLibrary Helper Script             #
# Author(s):     desimaniac                                             #
# URL:           https://github.com/cloudbox2/cloudbox                  #
# --                                                                    #
#         Part of the Cloudbox project: https://cloudbox.works          #
#########################################################################
#                   GNU General Public License v3.0                     #
#########################################################################

PATH='/usr/bin:/bin:/usr/local/bin'
export PYTHONIOENCODING=UTF-8

PYTHON_PATH='/usr/bin/python'
LOG_PATH='/opt/python-plexlibrary/plexlibrary.log'
RECIPES_PATH='/opt/python-plexlibrary/recipes'
SCRIPT_PATH='/opt/python-plexlibrary/plexlibrary/plexlibrary.py'

echo $(date) | tee -a $LOG_PATH
echo "" | tee -a $LOG_PATH

$PYTHON_PATH $SCRIPT_PATH $RECIPES_PATH/"$@"