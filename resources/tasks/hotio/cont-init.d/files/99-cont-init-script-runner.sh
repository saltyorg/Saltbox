#!/usr/bin/with-contenv bash

#########################################################################
# Title:         Saltbox: cont-init.d Script Runner                     #
# Author(s):     desimaniac                                             #
# URL:           https://github.com/saltyorg/Saltbox                    #
# --                                                                    #
#########################################################################
#                   GNU General Public License v3.0                     #
#########################################################################

scripts=$(find /config/script.d/ -name "*.sh" -type f | sort)

for script in $scripts; do
    echo "[cont-init.d script-runner] ${script}: executing... "
    bash $file
done